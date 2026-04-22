import json
import redis as redis_lib
from fastapi import APIRouter
from app.config import settings
from app.utils.config_store import is_configured, get
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger("api.status")


@router.get("/api/status")
async def get_status():
    """
    Returns connectivity status for Lago and Paddle.
    Used by the status page to show live health cards.
    """
    configured = is_configured()
    lago   = {"ok": False, "error": None}
    paddle = {"ok": False, "error": None}

    if configured:
        # ── Lago ping ──
        try:
            from app.clients.lago import LagoClient
            client = LagoClient()
            resp = await client._client.get("/customers?per_page=1")
            await client.close()
            if resp.is_success:
                lago["ok"] = True
            else:
                lago["error"] = f"HTTP {resp.status_code}"
        except Exception as exc:
            lago["error"] = str(exc)

        # ── Paddle ping ──
        try:
            from app.clients.paddle_classic import PaddleClassicClient
            paddle_client = PaddleClassicClient()
            resp = await paddle_client._client.post(
                f"{paddle_client._base_url}/subscription/plans",
                data=paddle_client._auth(),
            )
            await paddle_client.close()
            data = resp.json()
            if data.get("success"):
                paddle["ok"] = True
            else:
                paddle["error"] = data.get("error", "Unknown error")
        except Exception as exc:
            paddle["error"] = str(exc)

    return {
        "configured": configured,
        "lago":   lago,
        "paddle": paddle,
    }


@router.get("/api/logs")
async def get_logs(limit: int = 200):
    """
    Return recent log entries from the Redis circular buffer.
    Newest entries first (LPUSH order).
    """
    try:
        r = redis_lib.from_url(settings.redis_url, decode_responses=True)
        raw = r.lrange("middleware:logs", 0, min(limit, 500) - 1)
        entries = []
        for line in raw:
            try:
                entries.append(json.loads(line))
            except Exception:
                entries.append({"event": line, "level": "info", "timestamp": ""})
        return {"logs": entries}
    except Exception as exc:
        return {"logs": [], "error": str(exc)}
