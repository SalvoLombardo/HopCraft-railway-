"""
Shared retry helper for external HTTP calls (flight providers and LLMs).

Retries with exponential backoff + jitter on transient failures:
  - httpx.TimeoutException / httpx.TransportError (network blips)
  - HTTP status codes in retry_statuses (default: 429 + common 5xx)

Non-transient responses (4xx other than 429) are returned as-is: the caller
decides how to handle them. After the last attempt the response is returned
(if any) or the last exception re-raised, so callers keep full control over
error handling and the provider cascade can move on.

Design notes:
  - Jitter avoids thundering-herd retries when many parallel calls (e.g. one
    per date in SerpAPI) hit a rate limit at the same moment.
  - LLM callers should pass retry_statuses without 429: a rate-limited LLM is
    better handled by falling back to the next provider in the chain than by
    waiting out the backoff.
"""
import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)


async def request_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    attempts: int = 3,
    retry_statuses: tuple[int, ...] = DEFAULT_RETRY_STATUSES,
    base_delay: float = 1.0,
    label: str = "",
) -> httpx.Response:
    """
    Executes `send()` with up to `attempts` tries and exponential backoff
    (base_delay * 2^attempt, plus 0–25% jitter) between tries.

    Returns the last httpx.Response (even if its status is in retry_statuses —
    the caller still does raise_for_status / status handling).
    Raises the last network exception if every attempt failed at transport level.
    """
    last_exc: Exception | None = None
    resp: httpx.Response | None = None

    for attempt in range(attempts):
        try:
            resp = await send()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            logger.warning(
                "%s: %s (attempt %d/%d)",
                label or "HTTP call", type(exc).__name__, attempt + 1, attempts,
            )
            resp = None
        else:
            if resp.status_code not in retry_statuses:
                return resp
            logger.warning(
                "%s: HTTP %d (attempt %d/%d)",
                label or "HTTP call", resp.status_code, attempt + 1, attempts,
            )

        if attempt < attempts - 1:
            delay = base_delay * (2 ** attempt)
            delay += delay * random.uniform(0, 0.25)
            await asyncio.sleep(delay)

    if resp is not None:
        return resp
    assert last_exc is not None
    raise last_exc
