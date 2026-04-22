"""
Durable config store for runtime setup configuration.

Two-layer storage:
  1. Redis hash  — fast path, shared between API and Celery worker
  2. /data/config.json on a named Docker volume — durable fallback

Write path:  save() → Redis + file (both updated together)
Read path:   get()  → Redis first; if Redis is down, read from file
Startup:     rehydrate_redis() loads file → Redis when the hash is missing
             (called from FastAPI startup event; Celery uses the file fallback directly)

This means:
  - Redis restart  → rehydrated from file on next API startup, zero data loss
  - Redis down mid-flight → reads fall back to file automatically
  - File deleted  → Redis still works until next restart
  - Both gone     → need to re-run setup (acceptable; that's a full data loss event)
"""
import json
import redis as redis_lib
from pathlib import Path
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("config_store")

_REDIS_KEY = "middleware:config"
_CONFIG_FILE = Path("/data/config.json")

_REQUIRED_KEYS = {
    "LAGO_API_URL",
    "LAGO_API_KEY",
    "LAGO_WEBHOOK_SECRET",
    "LAGO_PLAN_CODE",
    "PADDLE_CLASSIC_URL",
    "PADDLE_VENDOR_ID",
    "PADDLE_VENDOR_AUTH_CODE",
    "PADDLE_MONTHLY_PLAN_ID",
}


# ── Internals ──────────────────────────────────────────────────────────────

def _redis() -> redis_lib.Redis:
    return redis_lib.from_url(settings.redis_url, decode_responses=True)


def _read_file() -> dict:
    try:
        if _CONFIG_FILE.exists():
            return json.loads(_CONFIG_FILE.read_text())
    except Exception as exc:
        logger.warning("config file read failed", error=str(exc))
    return {}


def _write_file(values: dict) -> None:
    try:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(json.dumps(values, indent=2))
    except Exception as exc:
        # Don't let a file write failure break setup — Redis still has it
        logger.warning("config file write failed", error=str(exc))


# ── Public API ─────────────────────────────────────────────────────────────

def save(values: dict) -> None:
    """
    Persist setup config. Writes to Redis and the durable file.
    Merges with existing file content so partial updates don't lose other keys.
    Keys explicitly passed as empty string are deleted (allows clearing stale values
    like LAGO_API_HOST when switching from local Docker to Lago Cloud).
    """
    to_set    = {k: v for k, v in values.items() if v}
    to_delete = [k for k, v in values.items() if v == ""]

    # Merge so a re-run of setup only overwrites what was sent
    on_disk = _read_file()
    on_disk.update(to_set)
    for k in to_delete:
        on_disk.pop(k, None)

    try:
        r = _redis()
        if to_set:
            r.hset(_REDIS_KEY, mapping=to_set)
        if to_delete:
            r.hdel(_REDIS_KEY, *to_delete)
    except Exception as exc:
        logger.warning("redis write failed during save", error=str(exc))

    _write_file(on_disk)
    if to_set:
        logger.info("config saved", keys=sorted(to_set.keys()))
    if to_delete:
        logger.info("config keys cleared", keys=sorted(to_delete))


def get(key: str) -> str | None:
    """Fetch one value. Redis first; file fallback if Redis is unreachable."""
    try:
        val = _redis().hget(_REDIS_KEY, key)
        if val is not None:
            return val
    except Exception:
        pass
    return _read_file().get(key)


def all_values() -> dict:
    """Fetch all config. Redis first; file fallback."""
    try:
        vals = _redis().hgetall(_REDIS_KEY)
        if vals:
            return vals
    except Exception:
        pass
    return _read_file()


def is_configured() -> bool:
    """True when all required setup keys are present."""
    cfg = all_values()
    return all(cfg.get(k) for k in _REQUIRED_KEYS)


def rehydrate_redis() -> None:
    """
    Load file → Redis if the Redis hash is missing or empty.
    Call this at API startup so a Redis restart is self-healing.
    """
    try:
        r = _redis()
        if r.exists(_REDIS_KEY):
            logger.info("redis config already present, skipping rehydration")
            return
        file_cfg = _read_file()
        if file_cfg:
            r.hset(_REDIS_KEY, mapping=file_cfg)
            logger.info("redis rehydrated from file", keys=sorted(file_cfg.keys()))
        else:
            logger.info("no config file found — fresh install, setup required")
    except Exception as exc:
        logger.warning("redis rehydration failed", error=str(exc))
