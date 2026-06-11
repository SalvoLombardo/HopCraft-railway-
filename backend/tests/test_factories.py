"""
Unit tests for the two Strategy layers:
  - Flight provider cascade (app/services/providers/factory.py)
  - LLM fallback chain (app/services/llm/factory.py)

Both factories are tested directly with mocked providers — the engine-level
tests (test_search, test_itinerary) already cover their integration.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.services.llm.factory import generate_with_fallback
from app.services.providers.factory import PROVIDER_LIMITS, get_providers_in_order


# ---------------------------------------------------------------------------
# Flight provider cascade
# ---------------------------------------------------------------------------

def _quota(remaining: dict[str, int]):
    """Builds a get_remaining mock returning per-provider remaining quota."""
    async def fake(key: str, max_calls: int) -> int:
        name = key.split(":")[0]
        return remaining.get(name, max_calls)
    return fake


class TestProviderCascade:

    async def test_default_cascade_order(self):
        with patch.object(settings, "flight_provider", "cascade"), \
             patch.object(settings, "apify_api_token", "tok"), \
             patch("app.services.providers.factory.get_remaining", _quota({})), \
             patch("app.services.providers.factory.is_open", AsyncMock(return_value=False)):
            providers = await get_providers_in_order()

        assert [name for name, _ in providers] == ["serpapi", "amadeus", "apify"]

    async def test_forced_provider_moves_to_front(self):
        with patch.object(settings, "flight_provider", "amadeus"), \
             patch.object(settings, "apify_api_token", "tok"), \
             patch("app.services.providers.factory.get_remaining", _quota({})), \
             patch("app.services.providers.factory.is_open", AsyncMock(return_value=False)):
            providers = await get_providers_in_order()

        assert [name for name, _ in providers] == ["amadeus", "serpapi", "apify"]

    async def test_apify_excluded_without_token(self):
        with patch.object(settings, "flight_provider", "cascade"), \
             patch.object(settings, "apify_api_token", ""), \
             patch("app.services.providers.factory.get_remaining", _quota({})), \
             patch("app.services.providers.factory.is_open", AsyncMock(return_value=False)):
            providers = await get_providers_in_order()

        assert [name for name, _ in providers] == ["serpapi", "amadeus"]

    async def test_exhausted_provider_is_skipped(self):
        with patch.object(settings, "flight_provider", "cascade"), \
             patch.object(settings, "apify_api_token", ""), \
             patch("app.services.providers.factory.get_remaining",
                   _quota({"serpapi": 0})), \
             patch("app.services.providers.factory.is_open", AsyncMock(return_value=False)):
            providers = await get_providers_in_order()

        assert [name for name, _ in providers] == ["amadeus"]

    async def test_open_circuit_is_skipped_even_with_quota(self):
        async def circuit(name: str) -> bool:
            return name == "serpapi"

        with patch.object(settings, "flight_provider", "cascade"), \
             patch.object(settings, "apify_api_token", ""), \
             patch("app.services.providers.factory.get_remaining", _quota({})), \
             patch("app.services.providers.factory.is_open", circuit):
            providers = await get_providers_in_order()

        assert [name for name, _ in providers] == ["amadeus"]

    async def test_all_exhausted_returns_empty_list(self):
        exhausted = {name: 0 for name in PROVIDER_LIMITS}
        with patch.object(settings, "flight_provider", "cascade"), \
             patch.object(settings, "apify_api_token", "tok"), \
             patch("app.services.providers.factory.get_remaining", _quota(exhausted)), \
             patch("app.services.providers.factory.is_open", AsyncMock(return_value=False)):
            providers = await get_providers_in_order()

        assert providers == []


# ---------------------------------------------------------------------------
# LLM fallback chain
# ---------------------------------------------------------------------------

_LLM_ARGS = dict(
    origin="CTA",
    duration_days=10,
    budget_per_leg=100.0,
    season="summer",
    num_stops=2,
    available_airports=["ATH (Athens)", "BUD (Budapest)"],
)


def _llm_mocks(gemini=None, groq=None, mistral=None):
    """Builds a _PROVIDERS replacement; each arg is a generate_itineraries mock."""
    def make(generate):
        provider = AsyncMock()
        provider.generate_itineraries = generate
        return provider
    return {
        "gemini": lambda: make(gemini or AsyncMock()),
        "groq": lambda: make(groq or AsyncMock()),
        "mistral": lambda: make(mistral or AsyncMock()),
    }


class TestLLMFallback:

    async def test_primary_provider_success_no_fallback(self):
        gemini = AsyncMock(return_value=["itinerary"])
        groq = AsyncMock()

        with patch.object(settings, "llm_provider", "gemini"), \
             patch("app.services.llm.factory._PROVIDERS", _llm_mocks(gemini=gemini, groq=groq)):
            result = await generate_with_fallback(**_LLM_ARGS)

        assert result == ["itinerary"]
        gemini.assert_awaited_once()
        groq.assert_not_awaited()

    async def test_fallback_to_second_provider_on_failure(self):
        gemini = AsyncMock(side_effect=RuntimeError("rate limited"))
        groq = AsyncMock(return_value=["from groq"])

        with patch.object(settings, "llm_provider", "gemini"), \
             patch("app.services.llm.factory._PROVIDERS", _llm_mocks(gemini=gemini, groq=groq)):
            result = await generate_with_fallback(**_LLM_ARGS)

        assert result == ["from groq"]
        gemini.assert_awaited_once()
        groq.assert_awaited_once()

    async def test_falls_through_to_last_provider(self):
        gemini = AsyncMock(side_effect=RuntimeError("down"))
        groq = AsyncMock(side_effect=RuntimeError("down"))
        mistral = AsyncMock(return_value=["from mistral"])

        with patch.object(settings, "llm_provider", "gemini"), \
             patch("app.services.llm.factory._PROVIDERS",
                   _llm_mocks(gemini=gemini, groq=groq, mistral=mistral)):
            result = await generate_with_fallback(**_LLM_ARGS)

        assert result == ["from mistral"]

    async def test_all_providers_fail_raises_runtime_error(self):
        failing = AsyncMock(side_effect=RuntimeError("down"))

        with patch.object(settings, "llm_provider", "gemini"), \
             patch("app.services.llm.factory._PROVIDERS",
                   _llm_mocks(gemini=failing, groq=failing, mistral=failing)):
            with pytest.raises(RuntimeError, match="None of the LLM providers"):
                await generate_with_fallback(**_LLM_ARGS)

    async def test_chain_starts_at_configured_provider(self):
        """LLM_PROVIDER=groq → gemini is never tried, even if groq fails."""
        gemini = AsyncMock(return_value=["from gemini"])
        groq = AsyncMock(side_effect=RuntimeError("down"))
        mistral = AsyncMock(return_value=["from mistral"])

        with patch.object(settings, "llm_provider", "groq"), \
             patch("app.services.llm.factory._PROVIDERS",
                   _llm_mocks(gemini=gemini, groq=groq, mistral=mistral)):
            result = await generate_with_fallback(**_LLM_ARGS)

        assert result == ["from mistral"]
        gemini.assert_not_awaited()
