import hmac
import hashlib
import base64
from fastapi import Request, HTTPException
from app.utils.config_store import get
from app.utils.logger import get_logger

logger = get_logger("webhook.verify.lago")


async def verify_lago_signature(request: Request) -> bytes:
    raw_body = await request.body()

    signature = request.headers.get("x-lago-signature")
    algo = request.headers.get("x-lago-signature-algorithm", "hmac")

    if not signature:
        raise HTTPException(status_code=401, detail="Missing x-lago-signature header")

    secret = get("LAGO_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook secret not configured — run setup first")

    expected = base64.b64encode(
        hmac.new(
            secret.encode(),
            raw_body,
            hashlib.sha256,
        ).digest()
    ).decode()

    match = hmac.compare_digest(expected, signature)
    logger.info(
        "webhook signature check",
        algo=algo,
        received=signature[:20] + "...",
        expected=expected[:20] + "...",
        match=match,
        secret_prefix=secret[:8] + "...",
    )

    if not match:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    return raw_body
