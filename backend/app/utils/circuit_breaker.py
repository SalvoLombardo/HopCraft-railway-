"""
Redis-based circuit breaker for external flight providers.

Quota tracking (rate_limiter.py) protects against *exhausted* providers, but says
nothing about providers that are *failing* — e.g. an outage causing repeated 5xx.
Without a breaker, every search keeps paying the timeout cost on a dead provider
before falling back to the next one.

Behaviour (classic circuit breaker, simplified to open/closed):
  - Each failure increments "circuit:{name}:failures" (TTL = _FAILURE_WINDOW_SECONDS).
  - When failures reach _FAILURE_THRESHOLD, "circuit:{name}:open" is set with
    TTL = _COOLDOWN_SECONDS: the provider is skipped by the cascade.
  - When the cooldown expires the key disappears and the provider is retried
    (the half-open probe is implicit: first real call after cooldown).
  - A successful call resets the failure counter.

All operations are best-effort: if Redis is down, the breaker reports "closed"
and never blocks a call.
"""
import logging

from app.db.redis import get_redis

logger = logging.getLogger(__name__)

# Consecutive failures (within the window) that open the circuit.
_FAILURE_THRESHOLD = 3
# Window in which failures are counted.
_FAILURE_WINDOW_SECONDS = 120
# How long an open circuit stays open before the provider is retried.
_COOLDOWN_SECONDS = 300


def _failures_key(name: str) -> str:
    return f"circuit:{name}:failures"


def _open_key(name: str) -> str:
    return f"circuit:{name}:open"


async def is_open(name: str) -> bool:
    """True if the provider's circuit is open (provider should be skipped)."""
    try:
        redis = await get_redis()
        return bool(await redis.exists(_open_key(name)))
    except Exception as exc:
        logger.warning("Circuit breaker check failed for '%s': %s", name, exc)
        return False


async def record_failure(name: str) -> None:
    """Registers a provider failure; opens the circuit at the threshold."""
    try:
        redis = await get_redis()
        count = await redis.incr(_failures_key(name))
        if count == 1:
            await redis.expire(_failures_key(name), _FAILURE_WINDOW_SECONDS)
        if count >= _FAILURE_THRESHOLD:
            await redis.set(_open_key(name), "1", ex=_COOLDOWN_SECONDS)
            await redis.delete(_failures_key(name))
            logger.warning(
                "Circuit OPEN for provider '%s' after %d failures — cooling down %ds",
                name, count, _COOLDOWN_SECONDS,
            )
    except Exception as exc:
        logger.warning("Circuit breaker record_failure failed for '%s': %s", name, exc)


async def record_success(name: str) -> None:
    """Resets the failure counter after a successful provider call."""
    try:
        redis = await get_redis()
        await redis.delete(_failures_key(name))
    except Exception as exc:
        logger.warning("Circuit breaker record_success failed for '%s': %s", name, exc)
