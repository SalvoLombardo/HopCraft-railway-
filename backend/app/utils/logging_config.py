"""
Logging setup: structured JSON in production, human-readable in development.

Correlation ID:
  - The HTTP middleware in main.py stores a per-request ID in `correlation_id_var`
    (a contextvar). Contextvars propagate automatically to every coroutine/task
    spawned within the request (asyncio.gather included), so the whole 5-step
    pipeline and all provider/LLM calls share the same ID with no manual passing.
  - Every log record gets the ID injected by `_CorrelationFilter`, in both
    formats. Production emits one JSON object per line — easy to filter in
    Railway's log explorer (e.g. by correlation_id or level).
"""
import contextvars
import json
import logging

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(app_env: str) -> None:
    is_dev = app_env == "development"

    handler = logging.StreamHandler()
    handler.addFilter(_CorrelationFilter())
    if is_dev:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(correlation_id)s] %(name)s — %(message)s"
        ))
    else:
        handler.setFormatter(_JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if is_dev else logging.INFO)
