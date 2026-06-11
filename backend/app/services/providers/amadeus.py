"""
AmadeusProvider — primary provider (official API, stable).

Uses the Amadeus Self-Service API (free tier: 2,000 requests/month).
LIMITATION: the free tier does NOT include European low-cost carriers
(Ryanair, Wizz Air, easyJet). Covers major carriers only (Lufthansa,
Air France, Iberia, British Airways, etc.).

Token optimisation: the OAuth2 token (valid ~30 min) is cached at module
level to avoid an extra POST /oauth2/token on every search.
The async lock (_TOKEN_LOCK) serialises concurrent token requests,
preventing burst calls to the auth endpoint.

Rate limiting: the Amadeus test API has a limit of ~10 req/sec. On HTTP 429
search_one_way retries with exponential backoff (1s, 2s, 4s).

Documentation: https://developers.amadeus.com/self-service/category/flights
"""
import asyncio
import logging
import re
import time
from datetime import date

import httpx

from app.services.providers.base import FlightOffer, FlightProvider, Leg

logger = logging.getLogger(__name__)

_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
_SEARCH_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

# Module-level token cache: api_key → (token, expires_at_monotonic)
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# Lock to serialise concurrent token requests (lazy init).
# Prevents N parallel tasks from all requesting a token at the same time.
_TOKEN_LOCK: asyncio.Lock | None = None

# Global semaphore: caps total concurrent Amadeus search_one_way calls across
# all itineraries. Amadeus free tier allows ~10 req/s; keep well below to
# avoid 429 cascades that trigger exponential backoff and bust the 60s budget.
_MAX_CONCURRENT_AMADEUS_CALLS = 5
_AMADEUS_SEMAPHORE: asyncio.Semaphore | None = None


def _get_amadeus_semaphore() -> asyncio.Semaphore:
    global _AMADEUS_SEMAPHORE
    if _AMADEUS_SEMAPHORE is None:
        _AMADEUS_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_AMADEUS_CALLS)
    return _AMADEUS_SEMAPHORE


def _parse_iso_duration(duration: str) -> int:
    """Converts ISO 8601 duration 'PT2H30M' to total minutes."""
    hours = int(re.search(r"(\d+)H", duration).group(1)) if "H" in duration else 0
    mins = int(re.search(r"(\d+)M", duration).group(1)) if "M" in duration else 0
    return hours * 60 + mins


def _parse_offer(item: dict) -> FlightOffer | None:
    """Normalises an Amadeus offer into a FlightOffer."""
    try:
        itinerary = item["itineraries"][0]
        segments = itinerary["segments"]
        first_seg = segments[0]
        last_seg = segments[-1]

        return FlightOffer(
            origin=first_seg["departure"]["iataCode"],
            destination=last_seg["arrival"]["iataCode"],
            departure=first_seg["departure"]["at"],
            price_eur=float(item["price"]["total"]),
            airline=first_seg["carrierCode"],
            direct=(len(segments) == 1),
            duration_minutes=_parse_iso_duration(itinerary["duration"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


class AmadeusProvider(FlightProvider):

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        """Returns a valid OAuth2 token, using the cache when available.

        The lock serialises concurrent requests: only the first task calls
        the auth endpoint; the others wait and then find the token in cache.
        """
        global _TOKEN_LOCK
        if _TOKEN_LOCK is None:
            _TOKEN_LOCK = asyncio.Lock()

        async with _TOKEN_LOCK:
            now = time.monotonic()
            cached = _TOKEN_CACHE.get(self.api_key)
            if cached and now < cached[1] - 60:   # 60s safety margin before expiry
                return cached[0]

            resp = await client.post(
                _AUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.api_key,
                    "client_secret": self.api_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()
            token: str = data["access_token"]
            expires_in: int = int(data.get("expires_in", 1799))
            _TOKEN_CACHE[self.api_key] = (token, now + expires_in)
            return token

    async def search_one_way(
        self,
        origin: str,
        destination: str,
        date_from: date,
        date_to: date,
        direct_only: bool = False,
        max_results: int = 50,
    ) -> list[FlightOffer]:
        """Searches one-way flights with automatic retry on HTTP 429/5xx (backoff 1s, 2s, 4s)."""
        # Amadeus does not natively support date ranges:
        # date_from is used as the primary departure date
        params: dict = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": date_from.isoformat(),
            "adults": 1,
            "currencyCode": "EUR",
            "max": min(max_results, 250),  # Amadeus max is 250
        }
        if direct_only:
            params["nonStop"] = "true"

        for attempt in range(3):
            try:
                async with _get_amadeus_semaphore():
                    async with httpx.AsyncClient(timeout=30) as client:
                        token = await self._get_token(client)
                        resp = await client.get(
                            _SEARCH_URL,
                            params=params,
                            headers={"Authorization": f"Bearer {token}"},
                        )
                # client closed here — resp.json() still accessible (body already read by httpx)
            except httpx.TimeoutException:
                logger.warning(
                    "Amadeus %s→%s %s: timeout (attempt %d/3)",
                    origin, destination, date_from, attempt + 1,
                )
                if attempt == 2:
                    return []
                continue

            # Retry transient statuses: 429 (rate limit) and 5xx (server-side blips)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(
                    "Amadeus %s→%s %s: HTTP %d (attempt %d/3), retrying in %ds",
                    origin, destination, date_from, resp.status_code, attempt + 1, wait,
                )
                await asyncio.sleep(wait)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "Amadeus %s→%s %s: HTTP %d — %s",
                    origin, destination, date_from,
                    exc.response.status_code,
                    exc.response.text[:300],
                )
                return []

            data = resp.json().get("data", [])
            logger.debug("Amadeus %s→%s %s: %d offers", origin, destination, date_from, len(data))
            offers = [_parse_offer(item) for item in data]
            return [o for o in offers if o is not None]

        logger.warning(
            "Amadeus %s→%s %s: still failing after 3 attempts, leg skipped",
            origin, destination, date_from,
        )
        return []

    async def search_multi_city(
        self,
        legs: list[Leg],
    ) -> list[FlightOffer]:
        """Searches all legs in parallel and returns the cheapest offer per leg."""
        tasks = [
            self.search_one_way(leg.origin, leg.destination, leg.date, leg.date, max_results=5)
            for leg in legs
        ]
        results = await asyncio.gather(*tasks)
        offers: list[FlightOffer] = []
        for leg_offers in results:
            if leg_offers:
                offers.append(min(leg_offers, key=lambda o: o.price_eur))
        return offers
