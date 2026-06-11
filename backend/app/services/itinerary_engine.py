"""
Itinerary Engine — Smart Multi-City pipeline (2.3).

Orchestrates the full multi-city search in 5 steps:
  Step 1: calculate_area()         → radius, num_stops, reachable airports
  Step 2: generate_with_fallback() → candidate itineraries via AI (JSON)
  Step 3: real price check via FlightProvider cascade (parallel async calls)
  Step 4: budget filtering + ranking by price
  Step 5: return top 5 as SmartMultiOut
"""
import asyncio
import json
import logging
import time
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import ItineraryOut, LegOut, ProviderStatus, SmartMultiOut
from app.services.area_calculator import AreaResult, calculate_area
from app.services.llm.base import SuggestedItinerary
from app.services.llm.factory import generate_with_fallback
from app.services.providers.base import FlightOffer, Leg
from app.services.providers.factory import (
    MONTHLY_WINDOW,
    PROVIDER_LIMITS,
    PROVIDER_NOTES,
    get_provider_quotas,
    get_providers_in_order,
)
from app.utils.circuit_breaker import record_failure, record_success
from app.utils.llm_cache import get_cached_itineraries, save_itineraries
from app.utils.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)

# Hint injected into the LLM prompt only when Amadeus is the sole available provider.
# Amadeus free tier does not cover European low-cost carriers (Ryanair, easyJet, Wizz Air):
# the AI must be guided towards major-carrier hubs.
_AMADEUS_PROVIDER_HINT = (
    "The active flight provider covers major carriers only (Air France, Lufthansa, "
    "Iberia, BA, KLM, ITA, SAS, TAP, Finnair). Use ONLY major airports: "
    "CDG/ORY for Paris, FCO for Rome, LHR/LGW for London, MXP/LIN for Milan, "
    "AMS for Amsterdam, BRU for Brussels, MAD for Madrid, BCN for Barcelona, "
    "FRA for Frankfurt, MUC for Munich, VIE for Vienna, ZRH for Zurich. "
    "Avoid secondary airports: BGY, CIA, STN, BVA, CRL, HHN, EIN, MST, SXB, GDN."
)

# Maximum airports sent to the LLM (the closest ones, already sorted by distance)
_MAX_AIRPORTS_FOR_LLM = 50

# Maximum concurrent pricing tasks
_MAX_CONCURRENT_PRICING = 3


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _season_from_date(d: date) -> str:
    """Returns the season name for the given departure date."""
    month = d.month
    if month in (3, 4, 5):
        return "primavera"
    if month in (6, 7, 8):
        return "estate"
    if month in (9, 10, 11):
        return "autunno"
    return "inverno"


def _leg_dates(date_from: date, trip_duration_days: int, num_legs: int) -> list[date]:
    """
    Distributes leg departure dates evenly across the trip duration.
    """
    days_per_leg = trip_duration_days // num_legs
    return [date_from + timedelta(days=i * days_per_leg) for i in range(num_legs)]


def _days_per_stop(trip_duration_days: int, num_stops: int) -> list[int]:
    """Distributes trip days across intermediate stops."""
    if num_stops <= 0:
        return []
    base = trip_duration_days // num_stops
    remainder = trip_duration_days % num_stops
    return [base + (1 if i < remainder else 0) for i in range(num_stops)]


def _is_valid_route(route: list[str], origin: str) -> bool:
    """Validates the route structure returned by the AI."""
    if len(route) < 3:
        return False
    if route[0] != origin or route[-1] != origin:
        return False
    intermediate = route[1:-1]
    return len(intermediate) == len(set(intermediate))


# ---------------------------------------------------------------------------
# Step 3 helper — pricing a single itinerary with cascade provider
# ---------------------------------------------------------------------------

async def _price_itinerary(
    suggested: SuggestedItinerary,
    origin: str,
    date_from: date,
    trip_duration_days: int,
    direct_only: bool,
    semaphore: asyncio.Semaphore,
    providers_in_order: list,
) -> tuple[SuggestedItinerary, list[FlightOffer]] | None:
    """
    Fetches the cheapest price for each leg of the suggested itinerary
    using the provider cascade (SerpAPI → Amadeus).
    """
    if not _is_valid_route(suggested.route, origin):
        return None

    route = suggested.route
    num_legs = len(route) - 1
    dates = _leg_dates(date_from, trip_duration_days, num_legs)

    legs = [
        Leg(origin=route[i], destination=route[i + 1], date=dates[i])
        for i in range(num_legs)
    ]

    async with semaphore:
        offers: list[FlightOffer] = []
        for provider_name, provider in providers_in_order:
            rate_key = f"{provider_name}:monthly"
            allowed = await check_rate_limit(
                rate_key, PROVIDER_LIMITS[provider_name], MONTHLY_WINDOW
            )
            if not allowed:
                continue
            try:
                offers = await provider.search_multi_city(legs)
                await record_success(provider_name)
                if offers:
                    break
            except Exception as exc:
                logger.warning(
                    "Provider %s failed for itinerary %s: %s", provider_name, route, exc
                )
                await record_failure(provider_name)
                continue

    if len(offers) < num_legs:
        return None

    return (suggested, offers)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_smart_multi(
    session: AsyncSession,
    origin: str,
    trip_duration_days: int,
    budget_per_person_eur: float,
    travelers: int,
    date_from: date,
    date_to: date,
    direct_only: bool = False,
) -> SmartMultiOut:
    """
    Full Smart Multi-City pipeline.

    Returns:
        SmartMultiOut with the top 5 itineraries sorted by price + provider_status.
    """

    t_start = time.perf_counter()

    # ── Step 1: explorable area
    t1 = time.perf_counter()
    explorable_area_details: AreaResult = await calculate_area(session, origin, trip_duration_days)
    t_area_ms = int((time.perf_counter() - t1) * 1000)

    # ── Cascade provider setup (done before Step 2 to compute the provider_hint)
    providers_in_order = await get_providers_in_order()
    provider_names = [name for name, _ in providers_in_order]
    active_provider = provider_names[0] if provider_names else "none"

    # Fail fast if no flight provider is usable: pricing (step 3) would be
    # impossible, so don't burn an LLM call on step 2.
    if not providers_in_order:
        raise RuntimeError(
            "Nessun provider di voli è al momento disponibile "
            "(quote mensili esaurite o provider temporaneamente non raggiungibili). "
            "Riprova più tardi."
        )

    # provider_hint guides the AI only when Amadeus is the sole available provider
    only_amadeus = provider_names == ["amadeus"]
    provider_hint = _AMADEUS_PROVIDER_HINT if only_amadeus else ""

    # ── Step 2: itinerary generation via AI
    allowed_num_legs = explorable_area_details.num_stops + 1
    budget_per_leg = budget_per_person_eur / allowed_num_legs
    season = _season_from_date(date_from)

    airports_for_llm = explorable_area_details.airports[:_MAX_AIRPORTS_FOR_LLM]
    available_airports = [f"{a.iata_code} ({a.city})" for a in airports_for_llm]

    t2 = time.perf_counter()
    cache_args = (
        origin,
        trip_duration_days,
        budget_per_leg,
        season,
        explorable_area_details.num_stops,
        provider_hint,
    )
    suggestions = await get_cached_itineraries(*cache_args)
    llm_cache_hit = suggestions is not None
    if suggestions is None:
        suggestions = await generate_with_fallback(
            origin=origin,
            duration_days=trip_duration_days,
            budget_per_leg=budget_per_leg,
            season=season,
            num_stops=explorable_area_details.num_stops,
            available_airports=available_airports,
            provider_hint=provider_hint,
        )
        await save_itineraries(*cache_args, suggestions)
    t_llm_ms = int((time.perf_counter() - t2) * 1000)

    # ── Step 3: real price check (parallel, with concurrency limit)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PRICING)
    tasks = [
        _price_itinerary(
            s, origin, date_from, trip_duration_days, direct_only, semaphore, providers_in_order
        )
        for s in suggestions
    ]
    t3 = time.perf_counter()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    t_pricing_ms = int((time.perf_counter() - t3) * 1000)

    # ── Step 4: budget filtering + ranking
    n_no_data = 0
    n_over_budget = 0

    priced: list[tuple[SuggestedItinerary, list[FlightOffer], float]] = []
    for res in results:
        if res is None or isinstance(res, Exception):
            n_no_data += 1
            continue
        suggested, offers = res
        total_per_person = sum(o.price_eur for o in offers)
        if total_per_person > budget_per_person_eur:
            n_over_budget += 1
            continue
        priced.append((suggested, offers, total_per_person))

    priced.sort(key=lambda x: x[2])
    top5 = priced[:5]

    total_ms = int((time.perf_counter() - t_start) * 1000)
    logger.info(json.dumps({
        "event": "smart_multi_timing",
        "origin": origin,
        "trip_duration_days": trip_duration_days,
        "budget_eur": budget_per_person_eur,
        "travelers": travelers,
        "provider": active_provider,
        "llm_cache_hit": llm_cache_hit,
        "step_area_ms": t_area_ms,
        "step_llm_ms": t_llm_ms,
        "step_pricing_ms": t_pricing_ms,
        "routes_suggested": len(suggestions),
        "routes_no_data": n_no_data,
        "routes_over_budget": n_over_budget,
        "routes_returned": len(top5),
        "result": "success" if top5 else "no_results",
        "total_ms": total_ms,
    }))

    if not top5:
        if n_no_data > 0 and n_over_budget == 0:
            raise ValueError(
                f"Il provider non ha trovato voli per le rotte suggerite dall'AI "
                f"({n_no_data} itinerari senza copertura). "
                "Prova date diverse, un'origine con più connessioni, o cambia provider."
            )
        if n_over_budget > 0 and n_no_data == 0:
            raise ValueError(
                f"Trovati {n_over_budget} itinerari ma tutti oltre il budget di "
                f"€{budget_per_person_eur:.0f}/persona. "
                "Prova ad aumentare il budget o la durata del viaggio."
            )
        if n_no_data > 0 and n_over_budget > 0:
            raise ValueError(
                f"No valid itineraries: {n_no_data} without flight coverage, "
                f"{n_over_budget} over the budget of €{budget_per_person_eur:.0f}/person. "
                "Try different dates or increase the budget."
            )
        raise ValueError(
            "The AI did not generate valid itineraries for the provided parameters. "
            "Try a different origin or different dates."
        )

    # ── Step 5: build response
    itineraries: list[ItineraryOut] = []
    for rank, (suggested, offers, total_per_person) in enumerate(top5, start=1):
        legs_out = [
            LegOut(
                from_airport=o.origin,
                to_airport=o.destination,
                price_per_person_eur=o.price_eur,
                airline=o.airline,
                departure=o.departure,
                duration_minutes=o.duration_minutes,
                direct=o.direct,
            )
            for o in offers
        ]
        num_stops_in_route = len(suggested.route) - 2
        itineraries.append(
            ItineraryOut(
                rank=rank,
                route=suggested.route,
                total_price_per_person_eur=round(total_per_person, 2),
                total_price_all_travelers_eur=round(total_per_person * travelers, 2),
                legs=legs_out,
                ai_notes=suggested.reasoning,
                suggested_days_per_stop=_days_per_stop(trip_duration_days, num_stops_in_route),
            )
        )

    quotas = await get_provider_quotas()
    provider_status = ProviderStatus(
        active_provider=active_provider,
        serpapi_remaining=quotas.get("serpapi", 0),
        amadeus_remaining=quotas.get("amadeus", 0),
        note=PROVIDER_NOTES.get(active_provider, ""),
    )

    return SmartMultiOut(origin=origin, itineraries=itineraries, provider_status=provider_status)
