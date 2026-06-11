"""
Redis cache for LLM-generated itinerary suggestions (Smart Multi-City pipeline, step 2).

LLM free tiers (Gemini, Groq, Mistral) have limited daily/monthly quotas. Requests that
share the same search parameters (origin, trip duration, budget per leg, season, number
of stops, and provider hint) would otherwise trigger an identical LLM call every time —
caching the raw suggestions avoids that.

Note: this caches the AI's *route suggestions*, not flight prices. Prices are still
fetched live in step 3 of the pipeline, so cached itineraries are re-priced on every
request.
"""
import hashlib
import json
import logging
from dataclasses import asdict

from app.config import settings
from app.db.redis import get_redis
from app.services.llm.base import SuggestedItinerary

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "llm_itineraries"

# Round the per-leg budget to the nearest €25 so that small variations
# (a few cents/euros) share the same cache entry.
_BUDGET_BUCKET_EUR = 25


def _cache_key(
    origin: str,
    duration_days: int,
    budget_per_leg: float,
    season: str,
    num_stops: int,
    provider_hint: str,
) -> str:
    rounded_budget = round(budget_per_leg / _BUDGET_BUCKET_EUR) * _BUDGET_BUCKET_EUR
    payload = json.dumps(
        {
            "origin": origin,
            "duration_days": duration_days,
            "budget_per_leg": rounded_budget,
            "season": season,
            "num_stops": num_stops,
            "provider_hint": provider_hint,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"{_CACHE_PREFIX}:{digest}"


async def get_cached_itineraries(
    origin: str,
    duration_days: int,
    budget_per_leg: float,
    season: str,
    num_stops: int,
    provider_hint: str,
) -> list[SuggestedItinerary] | None:
    """Returns cached AI suggestions for these parameters, or None on a cache miss
    (or if Redis is unavailable — caching is best-effort and must never break the
    pipeline)."""
    try:
        redis = await get_redis()
        raw = await redis.get(
            _cache_key(origin, duration_days, budget_per_leg, season, num_stops, provider_hint)
        )
    except Exception as exc:
        logger.warning("LLM cache read failed: %s: %s", type(exc).__name__, exc)
        return None

    if raw is None:
        return None

    return [SuggestedItinerary(**item) for item in json.loads(raw)]


async def save_itineraries(
    origin: str,
    duration_days: int,
    budget_per_leg: float,
    season: str,
    num_stops: int,
    provider_hint: str,
    suggestions: list[SuggestedItinerary],
) -> None:
    """Caches AI suggestions for these parameters for LLM_CACHE_TTL_HOURS.
    Best-effort: a Redis failure here must not break the pipeline."""
    if not suggestions:
        return

    try:
        redis = await get_redis()
        key = _cache_key(origin, duration_days, budget_per_leg, season, num_stops, provider_hint)
        payload = json.dumps([asdict(s) for s in suggestions])
        await redis.set(key, payload, ex=settings.llm_cache_ttl_hours * 3600)
    except Exception as exc:
        logger.warning("LLM cache write failed: %s: %s", type(exc).__name__, exc)
