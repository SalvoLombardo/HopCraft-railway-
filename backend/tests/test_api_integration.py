"""
API-level integration tests: real HTTP request → FastAPI app → route validation →
pipeline → JSON response. External boundaries (DB session, providers, LLM, Redis)
are mocked; everything in between runs for real, including the correlation-ID
middleware and the per-IP rate limit dependency.

Uses httpx.ASGITransport, which does NOT trigger the lifespan (no real DB/Redis
needed).
"""
from unittest.mock import AsyncMock, patch

import httpx

from app.db.database import get_session
from app.main import app
from tests.test_itinerary import (
    _make_area_result,
    _make_offers_for_route,
    _make_suggestion,
)

_SMART_BODY = {
    "origin": "CTA",
    "trip_duration_days": 12,
    "budget_per_person_eur": 300.0,
    "travelers": 1,
    "date_from": "2026-06-01",
    "date_to": "2026-06-13",
    "direct_only": False,
}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class TestSmartMultiEndpoint:

    async def test_full_pipeline_over_http(self):
        """Happy path: POST → 5-step pipeline → ranked itineraries as JSON."""
        suggestions = [_make_suggestion(["CTA", "ATH", "CTA"])]
        offers = _make_offers_for_route(["CTA", "ATH", "CTA"], price_per_leg=60.0)

        mock_provider = AsyncMock()
        mock_provider.search_multi_city = AsyncMock(return_value=offers)

        app.dependency_overrides[get_session] = lambda: AsyncMock()
        try:
            with patch("app.services.itinerary_engine.calculate_area",
                       new=AsyncMock(return_value=_make_area_result())), \
                 patch("app.services.itinerary_engine.generate_with_fallback",
                       new=AsyncMock(return_value=suggestions)), \
                 patch("app.services.itinerary_engine.get_providers_in_order",
                       new=AsyncMock(return_value=[("serpapi", mock_provider)])), \
                 patch("app.services.itinerary_engine.check_rate_limit",
                       new=AsyncMock(return_value=True)), \
                 patch("app.services.itinerary_engine.get_provider_quotas",
                       new=AsyncMock(return_value={"serpapi": 200, "amadeus": 1800})), \
                 patch("app.services.itinerary_engine.get_cached_itineraries",
                       new=AsyncMock(return_value=None)), \
                 patch("app.services.itinerary_engine.save_itineraries",
                       new=AsyncMock()), \
                 patch("app.utils.user_rate_limit.check_rate_limit",
                       new=AsyncMock(return_value=True)):

                async with _client() as client:
                    resp = await client.post("/api/v1/search/smart-multi", json=_SMART_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert data["origin"] == "CTA"
        assert len(data["itineraries"]) == 1
        itinerary = data["itineraries"][0]
        assert itinerary["rank"] == 1
        assert itinerary["route"] == ["CTA", "ATH", "CTA"]
        assert itinerary["total_price_per_person_eur"] == 120.0
        assert data["provider_status"]["active_provider"] == "serpapi"
        # Correlation-ID middleware ran
        assert "x-request-id" in resp.headers

    async def test_validation_error_returns_422(self):
        bad_body = {**_SMART_BODY, "trip_duration_days": 2}  # below minimum of 5

        app.dependency_overrides[get_session] = lambda: AsyncMock()
        try:
            with patch("app.utils.user_rate_limit.check_rate_limit",
                       new=AsyncMock(return_value=True)):
                async with _client() as client:
                    resp = await client.post("/api/v1/search/smart-multi", json=bad_body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422

    async def test_rate_limited_request_returns_429(self):
        app.dependency_overrides[get_session] = lambda: AsyncMock()
        try:
            with patch("app.utils.user_rate_limit.check_rate_limit",
                       new=AsyncMock(return_value=False)):
                async with _client() as client:
                    resp = await client.post("/api/v1/search/smart-multi", json=_SMART_BODY)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 429

    async def test_incoming_request_id_is_honoured(self):
        app.dependency_overrides[get_session] = lambda: AsyncMock()
        try:
            with patch("app.utils.user_rate_limit.check_rate_limit",
                       new=AsyncMock(return_value=True)):
                async with _client() as client:
                    resp = await client.post(
                        "/api/v1/search/smart-multi",
                        json={**_SMART_BODY, "trip_duration_days": 2},  # fast 422 path
                        headers={"X-Request-ID": "trace-me-123"},
                    )
        finally:
            app.dependency_overrides.clear()

        assert resp.headers["x-request-id"] == "trace-me-123"
