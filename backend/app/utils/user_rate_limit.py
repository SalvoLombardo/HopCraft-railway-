"""
Per-IP rate limiting for the public search endpoints.

The monthly quota tracking in rate_limiter.py protects the *provider* accounts,
but a single user could still burn the whole month's quota by spamming searches.
This module adds a per-client-IP hourly limit on top, enforced as a FastAPI
dependency.

Railway (like most PaaS) terminates TLS at a proxy, so the real client IP is in
X-Forwarded-For; request.client.host is the proxy's address. We take the first
(leftmost) entry, which is the original client.

Fail-open by design: if Redis is unreachable, requests are allowed — a rate
limiter outage must not take the API down.
"""
import logging

from fastapi import HTTPException, Request

from app.config import settings
from app.utils.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 3600


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _enforce(request: Request, endpoint: str, max_calls: int) -> None:
    ip = _client_ip(request)
    try:
        allowed = await check_rate_limit(
            f"user:{ip}:{endpoint}:hourly", max_calls, _WINDOW_SECONDS
        )
    except Exception as exc:
        logger.warning("User rate limit check failed (allowing request): %s", exc)
        return

    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                "Hai raggiunto il limite di ricerche per questa ora. "
                "Riprova più tardi."
            ),
        )


async def limit_reverse_search(request: Request) -> None:
    await _enforce(request, "reverse", settings.rate_limit_reverse_hourly)


async def limit_smart_multi(request: Request) -> None:
    await _enforce(request, "smart", settings.rate_limit_smart_hourly)
