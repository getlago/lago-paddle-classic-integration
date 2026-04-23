import json
import structlog

# Loggers whose entries are written to stdout but suppressed from the UI log panel.
# Add any internal/plumbing logger name here to keep the status page clean.
_UI_HIDDEN_LOGGERS = {"config_store"}

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis as redis_lib
            from app.config import settings
            _redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        except Exception:
            pass
    return _redis_client


def _redis_log_processor(logger, method: str, event_dict: dict) -> dict:
    """Push each log entry to a Redis circular buffer for the status page."""
    if event_dict.get("logger") not in _UI_HIDDEN_LOGGERS:
        try:
            r = _get_redis()
            if r:
                r.lpush("middleware:logs", json.dumps(event_dict, default=str))
                r.ltrim("middleware:logs", 0, 499)   # keep newest 500
        except Exception:
            pass  # never let logging break the app
    return event_dict


structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _redis_log_processor,
        structlog.dev.ConsoleRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False,
)


def get_logger(name: str):
    return structlog.get_logger().bind(logger=name)
