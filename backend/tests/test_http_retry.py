"""
Unit tests for the shared HTTP retry helper (app/utils/http_retry.py).
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.utils.http_retry import request_with_retry


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "https://example.test"))


# Patch sleep everywhere so backoff doesn't slow the suite down
def _no_sleep():
    return patch("app.utils.http_retry.asyncio.sleep", AsyncMock())


class TestRequestWithRetry:
    async def test_success_first_try_no_retry(self):
        send = AsyncMock(return_value=_response(200))

        resp = await request_with_retry(send)

        assert resp.status_code == 200
        send.assert_awaited_once()

    async def test_retries_on_retryable_status_then_succeeds(self):
        send = AsyncMock(side_effect=[_response(429), _response(200)])

        with _no_sleep():
            resp = await request_with_retry(send)

        assert resp.status_code == 200
        assert send.await_count == 2

    async def test_retries_on_timeout_then_succeeds(self):
        send = AsyncMock(side_effect=[httpx.ConnectTimeout("slow"), _response(200)])

        with _no_sleep():
            resp = await request_with_retry(send)

        assert resp.status_code == 200
        assert send.await_count == 2

    async def test_returns_last_response_after_exhausted_attempts(self):
        send = AsyncMock(return_value=_response(503))

        with _no_sleep():
            resp = await request_with_retry(send, attempts=3)

        assert resp.status_code == 503
        assert send.await_count == 3

    async def test_raises_last_exception_when_all_attempts_fail_at_transport(self):
        send = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with _no_sleep():
            with pytest.raises(httpx.ConnectError):
                await request_with_retry(send, attempts=2)

        assert send.await_count == 2

    async def test_non_retryable_4xx_returned_immediately(self):
        send = AsyncMock(return_value=_response(404))

        resp = await request_with_retry(send)

        assert resp.status_code == 404
        send.assert_awaited_once()

    async def test_custom_retry_statuses_exclude_429(self):
        """LLM callers exclude 429 so the provider fallback chain kicks in immediately."""
        send = AsyncMock(return_value=_response(429))

        resp = await request_with_retry(send, retry_statuses=(500, 502, 503, 504))

        assert resp.status_code == 429
        send.assert_awaited_once()
