"""
Unit tests for the Redis-backed LLM itinerary cache (app/utils/llm_cache.py).
"""
from unittest.mock import AsyncMock, patch

from app.services.llm.base import SuggestedItinerary
from app.utils.llm_cache import get_cached_itineraries, save_itineraries


def _suggestion() -> SuggestedItinerary:
    return SuggestedItinerary(
        route=["CTA", "ATH", "SOF", "CTA"],
        reasoning="Cheap Balkan loop in shoulder season.",
        estimated_difficulty="medium",
        best_season=["apr", "may"],
    )


class TestLLMCache:
    async def test_cache_miss_returns_none(self):
        redis_mock = AsyncMock()
        redis_mock.get.return_value = None

        with patch("app.utils.llm_cache.get_redis", AsyncMock(return_value=redis_mock)):
            result = await get_cached_itineraries("CTA", 7, 200.0, "spring", 2, "")

        assert result is None

    async def test_save_then_get_round_trips(self):
        store: dict[str, str] = {}

        redis_mock = AsyncMock()
        redis_mock.set.side_effect = lambda key, value, ex=None: store.__setitem__(key, value)
        redis_mock.get.side_effect = lambda key: store.get(key)

        with patch("app.utils.llm_cache.get_redis", AsyncMock(return_value=redis_mock)):
            await save_itineraries("CTA", 7, 200.0, "spring", 2, "", [_suggestion()])
            result = await get_cached_itineraries("CTA", 7, 200.0, "spring", 2, "")

        assert result == [_suggestion()]
        redis_mock.set.assert_awaited_once()
        _, kwargs = redis_mock.set.call_args
        assert kwargs["ex"] > 0

    async def test_budget_bucketing_shares_cache_entry(self):
        """Small budget differences (within €25) should hit the same cache entry."""
        store: dict[str, str] = {}

        redis_mock = AsyncMock()
        redis_mock.set.side_effect = lambda key, value, ex=None: store.__setitem__(key, value)
        redis_mock.get.side_effect = lambda key: store.get(key)

        with patch("app.utils.llm_cache.get_redis", AsyncMock(return_value=redis_mock)):
            await save_itineraries("CTA", 7, 200.0, "spring", 2, "", [_suggestion()])
            result = await get_cached_itineraries("CTA", 7, 210.0, "spring", 2, "")

        assert result == [_suggestion()]

    async def test_save_empty_suggestions_is_noop(self):
        redis_mock = AsyncMock()

        with patch("app.utils.llm_cache.get_redis", AsyncMock(return_value=redis_mock)):
            await save_itineraries("CTA", 7, 200.0, "spring", 2, "", [])

        redis_mock.set.assert_not_called()

    async def test_redis_failure_is_non_fatal(self):
        redis_mock = AsyncMock()
        redis_mock.get.side_effect = ConnectionError("redis down")
        redis_mock.set.side_effect = ConnectionError("redis down")

        with patch("app.utils.llm_cache.get_redis", AsyncMock(return_value=redis_mock)):
            result = await get_cached_itineraries("CTA", 7, 200.0, "spring", 2, "")
            await save_itineraries("CTA", 7, 200.0, "spring", 2, "", [_suggestion()])

        assert result is None
