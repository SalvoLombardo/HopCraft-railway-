"""
Unit tests for the provider circuit breaker (app/utils/circuit_breaker.py)
and for the per-IP user rate limiting dependency (app/utils/user_rate_limit.py).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.utils import circuit_breaker
from app.utils.circuit_breaker import is_open, record_failure, record_success
from app.utils.user_rate_limit import _client_ip, limit_reverse_search, limit_smart_multi


class _FakeRedis:
    """Minimal in-memory async Redis double (incr/expire/set/delete/exists)."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, 0)) + 1)
        return int(self.store[key])

    async def expire(self, key, seconds):
        return True

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def get(self, key):
        return self.store.get(key)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    async def test_circuit_closed_by_default(self):
        redis = _FakeRedis()
        with patch("app.utils.circuit_breaker.get_redis", AsyncMock(return_value=redis)):
            assert await is_open("serpapi") is False

    async def test_circuit_opens_after_threshold_failures(self):
        redis = _FakeRedis()
        with patch("app.utils.circuit_breaker.get_redis", AsyncMock(return_value=redis)):
            for _ in range(circuit_breaker._FAILURE_THRESHOLD):
                await record_failure("serpapi")
            assert await is_open("serpapi") is True
            # Failure counter is reset once the circuit opens
            assert "circuit:serpapi:failures" not in redis.store

    async def test_below_threshold_keeps_circuit_closed(self):
        redis = _FakeRedis()
        with patch("app.utils.circuit_breaker.get_redis", AsyncMock(return_value=redis)):
            for _ in range(circuit_breaker._FAILURE_THRESHOLD - 1):
                await record_failure("amadeus")
            assert await is_open("amadeus") is False

    async def test_success_resets_failure_counter(self):
        redis = _FakeRedis()
        with patch("app.utils.circuit_breaker.get_redis", AsyncMock(return_value=redis)):
            for _ in range(circuit_breaker._FAILURE_THRESHOLD - 1):
                await record_failure("serpapi")
            await record_success("serpapi")
            # The counter restarted: one more failure must NOT open the circuit
            await record_failure("serpapi")
            assert await is_open("serpapi") is False

    async def test_circuits_are_independent_per_provider(self):
        redis = _FakeRedis()
        with patch("app.utils.circuit_breaker.get_redis", AsyncMock(return_value=redis)):
            for _ in range(circuit_breaker._FAILURE_THRESHOLD):
                await record_failure("serpapi")
            assert await is_open("serpapi") is True
            assert await is_open("amadeus") is False

    async def test_redis_failure_is_non_fatal_and_fails_closed(self):
        broken = AsyncMock()
        broken.exists.side_effect = ConnectionError("redis down")
        broken.incr.side_effect = ConnectionError("redis down")
        broken.delete.side_effect = ConnectionError("redis down")

        with patch("app.utils.circuit_breaker.get_redis", AsyncMock(return_value=broken)):
            assert await is_open("serpapi") is False  # treated as closed
            await record_failure("serpapi")  # must not raise
            await record_success("serpapi")  # must not raise


# ---------------------------------------------------------------------------
# Per-IP user rate limiting
# ---------------------------------------------------------------------------

def _make_request(headers: dict | None = None, client_host: str = "10.0.0.1") -> MagicMock:
    request = MagicMock()
    request.headers = headers or {}
    request.client.host = client_host
    return request


class TestUserRateLimit:
    def test_client_ip_prefers_x_forwarded_for(self):
        request = _make_request(headers={"x-forwarded-for": "203.0.113.7, 10.0.0.2"})
        assert _client_ip(request) == "203.0.113.7"

    def test_client_ip_falls_back_to_client_host(self):
        request = _make_request(client_host="192.168.1.10")
        assert _client_ip(request) == "192.168.1.10"

    async def test_request_allowed_under_limit(self):
        with patch("app.utils.user_rate_limit.check_rate_limit",
                   AsyncMock(return_value=True)):
            await limit_reverse_search(_make_request())  # must not raise

    async def test_request_rejected_over_limit(self):
        with patch("app.utils.user_rate_limit.check_rate_limit",
                   AsyncMock(return_value=False)):
            with pytest.raises(HTTPException) as exc_info:
                await limit_smart_multi(_make_request())
        assert exc_info.value.status_code == 429

    async def test_redis_failure_fails_open(self):
        with patch("app.utils.user_rate_limit.check_rate_limit",
                   AsyncMock(side_effect=ConnectionError("redis down"))):
            await limit_reverse_search(_make_request())  # must not raise
