"""
GoogleFlightsProvider — primary provider via SerpAPI.

SerpAPI exposes Google Flights data (including European low-cost carriers:
Ryanair, Wizz Air, easyJet) as structured JSON without direct scraping.

Free tier: 100 searches/month — sufficient for development and portfolio demos.
Registration: https://serpapi.com

Endpoint documentation:
  https://serpapi.com/google-flights-api
"""
import asyncio
import logging
from datetime import date, timedelta

import httpx

from app.config import settings
from app.services.providers.base import FlightOffer, FlightProvider, Leg
from app.utils.http_retry import request_with_retry

logger = logging.getLogger(__name__)

_SERPAPI_URL = "https://serpapi.com/search.json"

# Maximum number of days in the search range to query in parallel
_MAX_DAYS_IN_RANGE = 7


def _parse_offer(item: dict, origin: str, destination: str) -> FlightOffer | None:
    """
    Normalises a SerpAPI offer (best_flights or other_flights) into a FlightOffer.

    SerpAPI structure:
    {
      "flights": [{"departure_airport": {...}, "arrival_airport": {...},
                   "airline": "Ryanair", "duration": 125, ...}],
      "total_duration": 125,
      "price": 29,
      ...
    }
    """
    try:
        flights = item.get("flights", [])
        if not flights:
            return None

        first_leg = flights[0]
        last_leg = flights[-1]

        departure_time = first_leg.get("departure_airport", {}).get("time", "")
        # SerpAPI returns times as "2026-04-01 07:15" — convert to ISO format
        departure_iso = departure_time.replace(" ", "T") if departure_time else ""

        return FlightOffer(
            origin=first_leg.get("departure_airport", {}).get("id", origin),
            destination=last_leg.get("arrival_airport", {}).get("id", destination),
            departure=departure_iso,
            price_eur=float(item.get("price", 0)),
            airline=first_leg.get("airline", "Unknown"),
            direct=(len(flights) == 1),
            duration_minutes=int(item.get("total_duration", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _fetch_for_date(
    origin: str,
    destination: str,
    search_date: date,
    direct_only: bool,
    max_results: int,
) -> list[FlightOffer]:
    """Calls SerpAPI for a single date and returns normalised FlightOffer objects."""
    params: dict = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": search_date.isoformat(),
        "currency": "EUR",
        "hl": "en",
        "type": "2",      # 1=round-trip, 2=one-way
        "adults": "1",
        "api_key": settings.serpapi_api_key,
    }
    if direct_only:
        params["stops"] = "0"   # 0=direct only, 1=max 1 stop, 2=any

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await request_with_retry(
            lambda: client.get(_SERPAPI_URL, params=params),
            label=f"SerpAPI {origin}→{destination} {search_date}",
        )
        resp.raise_for_status()
        data = resp.json()

    offers: list[FlightOffer] = []
    # SerpAPI splits results into best_flights and other_flights
    for section in ("best_flights", "other_flights"):
        for item in data.get(section, []):
            offer = _parse_offer(item, origin, destination)
            if offer:
                offers.append(offer)

    offers.sort(key=lambda o: o.price_eur)
    return offers[:max_results]


class GoogleFlightsProvider(FlightProvider):

    async def search_one_way(
        self,
        origin: str,
        destination: str,
        date_from: date,
        date_to: date,
        direct_only: bool = False,
        max_results: int = 50,
    ) -> list[FlightOffer]:
        # Build the list of dates in the range (max _MAX_DAYS_IN_RANGE)
        dates: list[date] = []
        current = date_from
        while current <= date_to and len(dates) < _MAX_DAYS_IN_RANGE:
            dates.append(current)
            current += timedelta(days=1)

        # Parallel calls — one per date
        tasks = [
            _fetch_for_date(origin, destination, d, direct_only, max_results)
            for d in dates
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        offers: list[FlightOffer] = []
        for d, r in zip(dates, results):
            if isinstance(r, list):
                offers.extend(r)
            else:
                logger.warning(
                    "SerpAPI %s→%s %s: date skipped — %s: %s",
                    origin, destination, d, type(r).__name__, r,
                )

        offers.sort(key=lambda o: o.price_eur)
        return offers[:max_results]

    async def search_multi_city(
        self,
        legs: list[Leg],
    ) -> list[FlightOffer]:
        """Searches the cheapest offer for each leg in parallel."""
        tasks = [
            _fetch_for_date(leg.origin, leg.destination, leg.date, False, 5)
            for leg in legs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        offers: list[FlightOffer] = []
        for leg, r in zip(legs, results):
            if isinstance(r, list) and r:
                offers.append(min(r, key=lambda o: o.price_eur))
            elif isinstance(r, Exception):
                logger.warning(
                    "SerpAPI multi-city %s→%s %s: leg failed — %s: %s",
                    leg.origin, leg.destination, leg.date, type(r).__name__, r,
                )

        return offers
