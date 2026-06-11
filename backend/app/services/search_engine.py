"""
Core logic for Reverse Search.

Flow:
  1. Efficient batch query: finds all valid cache entries for
     (any_origin → destination) on the requested dates.
  2. For airports with no cache hit, calls providers in cascade order
     (SerpAPI → Amadeus) until one returns results.
     Maximum _MAX_NEW_CALLS_PER_SEARCH airports per search.
  3. Saves new results to cache.
  4. Returns an enriched list with airport coordinates + provider metadata.

Monthly rate limiting is managed via Redis: separate key per provider
(serpapi:monthly, amadeus:monthly). Limits and the time window
are centralised in providers/factory.py (PROVIDER_LIMITS, MONTHLY_WINDOW).
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings  # noqa: F401 — kept for existing import compatibility
from app.models.airport import Airport
from app.models.flight_cache import FlightCache
from app.db.cache import save_to_cache
from app.models.schemas import ProviderStatus
from app.services.providers.base import FlightOffer
from app.services.providers.factory import (
    MONTHLY_WINDOW,
    PROVIDER_LIMITS,
    PROVIDER_NOTES,
    get_provider_quotas,
    get_providers_in_order,
)
from app.utils.circuit_breaker import record_failure, record_success
from app.utils.geo import haversine_km
from app.utils.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)

# Maximum new provider calls per single search
_MAX_NEW_CALLS_PER_SEARCH = 50


def _cache_cutoff() -> datetime:
    from app.config import settings as _s
    return (datetime.now(timezone.utc) - timedelta(hours=_s.cache_ttl_hours)).replace(tzinfo=None)


async def reverse_search(
    session: AsyncSession,
    destination: str,
    date_from: date,
    date_to: date,
    direct_only: bool = False,
    max_results: int = 50,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    radius_km: int | None = None,
) -> tuple[list[dict], bool, datetime, ProviderStatus]:
    """
    Reverse search: finds the cheapest flights to destination from all active airports.

    Optional geographic filter parameters:
        origin_lat / origin_lon / radius_km — restricts the search to airports
        within radius_km of the given point.

    Returns:
        (results list, all_from_cache, fetched_at, provider_status)
    """

    # --- 1. Load all active airports (excluding the destination itself)
    stmt_airports = select(Airport).where(
        Airport.is_active.is_(True),
        Airport.iata_code != destination,
    )
    airport_rows = await session.execute(stmt_airports)
    airports: list[Airport] = list(airport_rows.scalars().all())
    airport_map: dict[str, Airport] = {a.iata_code: a for a in airports}

    # --- 1b. Optional geographic radius filter
    if origin_lat is not None and origin_lon is not None and radius_km is not None:
        airport_map = {
            code: airport
            for code, airport in airport_map.items()
            if haversine_km(
                origin_lat, origin_lon, airport.latitude, airport.longitude
            ) <= radius_km
        }

    # --- 2. Building 7days range
    date_list: list[date] = []
    current = date_from
    while current <= date_to and len(date_list) < 7:
        date_list.append(current)
        current += timedelta(days=1)

    # --- 3. Checking in cache for (date_list)
    stmt_cache = select(FlightCache).where(
        FlightCache.destination == destination,
        FlightCache.departure_date.in_(date_list),
        FlightCache.fetched_at >= _cache_cutoff(),
    )
    cache_rows = await session.execute(stmt_cache)

    cache_best: dict[str, tuple[FlightOffer, datetime]] = {}
    for single_flight_cache_obj in cache_rows.scalars():
        offers = [FlightOffer(**item) for item in (single_flight_cache_obj.raw_response or [])]
        if not offers:
            continue
        cheapest = min(offers, key=lambda o: o.price_eur)
        prev = cache_best.get(single_flight_cache_obj.origin)
        if prev is None or cheapest.price_eur < prev[0].price_eur:
            cache_best[single_flight_cache_obj.origin] = (
                cheapest, single_flight_cache_obj.fetched_at
            )

    # --- 4. Missing origins
    all_origins = set(airport_map.keys())
    cached_origins = set(cache_best.keys())
    missing_origins = list(all_origins - cached_origins)[:_MAX_NEW_CALLS_PER_SEARCH]

    # --- 5. Cascade provider setup
    providers_in_order = await get_providers_in_order()
    active_provider = providers_in_order[0][0] if providers_in_order else "none"

    fresh_best: dict[str, FlightOffer] = {}

    async def _fetch(origin: str) -> None:
        for provider_name, provider in providers_in_order:
            rate_key = f"{provider_name}:monthly"
            allowed = await check_rate_limit(
                rate_key, PROVIDER_LIMITS[provider_name], MONTHLY_WINDOW
            )
            if not allowed:
                continue
            try:
                offers = await provider.search_one_way(
                    origin, destination, date_from, date_to,
                    direct_only=direct_only, max_results=10,
                )
                await record_success(provider_name)
                if not offers:
                    continue

                # Save to cache per date
                for single_date in date_list:
                    day_offers = [
                        o for o in offers
                        if o.departure.startswith(single_date.isoformat())
                    ]
                    if day_offers:
                        await save_to_cache(session, origin, destination, single_date, day_offers)
                fresh_best[origin] = min(offers, key=lambda o: o.price_eur)
                return  # provider responded: skip remaining providers

            except Exception as exc:
                logger.warning(
                    "Provider %s %s→%s failed: %s: %s",
                    provider_name, origin, destination, type(exc).__name__, exc,
                )
                await record_failure(provider_name)
                continue  # try the next provider

    await asyncio.gather(*[_fetch(o) for o in missing_origins])

    # --- 6. Assembling the answer
    results: list[dict] = []

    for origin, (offer, fetched_at) in cache_best.items():
        airport = airport_map.get(origin)
        if airport:
            results.append(_build_result(offer, airport, fetched_at))

    now = datetime.now(timezone.utc)
    for origin, offer in fresh_best.items():
        airport = airport_map.get(origin)
        if airport:
            results.append(_build_result(offer, airport, now))

    results.sort(key=lambda r: r["price_eur"])
    results = results[:max_results]

    all_from_cache = len(fresh_best) == 0
    fetched_at = results[0]["_fetched_at"] if results else now

    for r in results:
        r.pop("_fetched_at")

    # --- 7. Provider status
    quotas = await get_provider_quotas()
    provider_status = ProviderStatus(
        active_provider=active_provider,
        serpapi_remaining=quotas.get("serpapi", 0),
        amadeus_remaining=quotas.get("amadeus", 0),
        note=PROVIDER_NOTES.get(active_provider, ""),
    )

    return results, all_from_cache, fetched_at, provider_status


def _build_result(offer: FlightOffer, airport: Airport, fetched_at: datetime) -> dict:
    return {
        "origin": offer.origin,
        "origin_city": airport.city,
        "price_eur": offer.price_eur,
        "airline": offer.airline,
        "departure": offer.departure,
        "direct": offer.direct,
        "duration_minutes": offer.duration_minutes,
        "latitude": airport.latitude,
        "longitude": airport.longitude,
        "_fetched_at": fetched_at,
    }
