import httpx
from urllib.parse import urlparse
from fastapi import APIRouter
from pydantic import BaseModel
from app.utils.logger import get_logger
from app.utils import config_store

router = APIRouter()
logger = get_logger("api.setup")


class SetupRequest(BaseModel):
    # Lago
    lago_api_url: str = "https://api.getlago.com/api/v1"
    lago_api_host: str = ""       # auto-derived when URL has an explicit port
    lago_api_key: str
    lago_webhook_secret: str      # Lago UI → Settings → Developers → Webhooks → HMAC signature

    # Paddle Classic
    paddle_classic_url: str = "https://sandbox-vendors.paddle.com/api/2.0"
    paddle_vendor_id: str
    paddle_vendor_auth_code: str
    # Plan ID created manually in the Paddle dashboard ($0/mo monthly)
    paddle_monthly_plan_id: str

    # App
    middleware_url: str        # URL Lago uses to deliver webhooks — must be publicly reachable (ngrok for local dev)
    paddle_public_key: str = ""  # RSA public key for Paddle webhook signature verification (optional but recommended)
    lago_plan_code: str = ""   # existing Lago plan code — if set, skips auto-creation of metric + plan


class SetupResponse(BaseModel):
    success: bool
    webhook_url: str
    webhook_already_registered: bool
    lago_plan_code: str
    paddle_monthly_plan_id: str
    message: str


@router.post("/api/setup", response_model=SetupResponse)
async def setup(req: SetupRequest):
    logger.info("setup started")

    # When the URL has an explicit port it's a direct container address.
    # Rails' HostAuthorization blocks it unless Host: api.lago.dev is set.
    _parsed = urlparse(req.lago_api_url)
    lago_api_host = req.lago_api_host or ("api.lago.dev" if _parsed.port else "")

    lago_headers = {"Authorization": f"Bearer {req.lago_api_key}"}
    if lago_api_host:
        lago_headers["Host"] = lago_api_host

    lago_base = req.lago_api_url.rstrip("/")
    middleware_url = req.middleware_url.rstrip("/")

    logger.info("lago request config", url=lago_base, host_header=lago_api_host or "(none)")

    # ── Step 1: Validate Lago credentials ──
    async with httpx.AsyncClient(timeout=15.0) as client:
        check = await client.get(f"{lago_base}/webhook_endpoints", headers=lago_headers)
        if not check.is_success:
            logger.error("lago auth check failed", status=check.status_code, body=check.text[:300])
        check.raise_for_status()
    logger.info("lago credentials validated")

    # ── Step 2: Register webhook endpoint in Lago ──
    webhook_url = f"{middleware_url}/webhooks/lago"
    existing_endpoints = check.json().get("webhook_endpoints", [])
    existing_urls = [ep.get("webhook_url") for ep in existing_endpoints]

    # Clean up stale middleware webhooks (same path, different host) before registering
    stale = [
        ep for ep in existing_endpoints
        if ep.get("webhook_url", "").endswith("/webhooks/lago")
        and ep.get("webhook_url") != webhook_url
    ]
    if stale:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for ep in stale:
                lago_id = ep.get("lago_id")
                del_resp = await client.delete(
                    f"{lago_base}/webhook_endpoints/{lago_id}",
                    headers=lago_headers,
                )
                if del_resp.is_success:
                    logger.info("stale lago webhook removed", old_url=ep.get("webhook_url"))
                else:
                    logger.warning("failed to remove stale webhook", lago_id=lago_id, status=del_resp.status_code)

    webhook_already_registered = webhook_url in existing_urls
    if not webhook_already_registered:
        async with httpx.AsyncClient(timeout=15.0) as client:
            reg = await client.post(
                f"{lago_base}/webhook_endpoints",
                headers=lago_headers,
                json={"webhook_endpoint": {"webhook_url": webhook_url, "signature_algo": "hmac"}},
            )
            reg.raise_for_status()
        logger.info("lago webhook registered", url=webhook_url)
    else:
        logger.info("lago webhook already registered", url=webhook_url)

    # ── Step 3: Validate Paddle credentials ──
    paddle_auth = {"vendor_id": req.paddle_vendor_id, "vendor_auth_code": req.paddle_vendor_auth_code}
    async with httpx.AsyncClient(timeout=15.0) as client:
        validate = await client.post(f"{req.paddle_classic_url}/subscription/plans", data=paddle_auth)
        validate.raise_for_status()
        if not validate.json().get("success"):
            raise ValueError("Invalid Paddle Classic credentials")
    logger.info("paddle credentials validated")

    # ── Step 4: Create Lago billable metric + plan (skipped if client provides their own plan code) ──
    lago_plan_code = req.lago_plan_code.strip() or "ai_tokens_plan"

    if not req.lago_plan_code.strip():
        lago_metric_code = "ai_tokens"
        async with httpx.AsyncClient(base_url=lago_base, headers=lago_headers, timeout=15.0) as client:
            # Create metric (idempotent)
            metric_resp = await client.post(
                "/billable_metrics",
                json={"billable_metric": {
                    "name": "AI Tokens", "code": lago_metric_code,
                    "aggregation_type": "sum_agg", "field_name": "tokens",
                }},
            )
            if metric_resp.status_code not in (200, 201, 422):
                metric_resp.raise_for_status()

            # Resolve the metric's lago_id — needed for plan charge creation
            if metric_resp.status_code == 422:
                existing = await client.get(f"/billable_metrics/{lago_metric_code}")
                existing.raise_for_status()
                metric_lago_id = existing.json()["billable_metric"]["lago_id"]
            else:
                metric_lago_id = metric_resp.json()["billable_metric"]["lago_id"]
            logger.info("lago billable metric ready", code=lago_metric_code, lago_id=metric_lago_id)

            # Create plan (idempotent)
            plan_resp = await client.post(
                "/plans",
                json={"plan": {
                    "name": "AI Tokens Plan", "code": lago_plan_code,
                    "interval": "monthly", "amount_cents": 0, "amount_currency": "USD",
                    "pay_in_advance": False,
                    "charges": [{
                        "billable_metric_id": metric_lago_id,
                        "charge_model": "standard",
                        "pay_in_advance": False,
                        "properties": {"amount": "0"},
                    }],
                }},
            )
            if plan_resp.status_code not in (200, 201, 422):
                plan_resp.raise_for_status()
            logger.info("lago plan ready", code=lago_plan_code)
    else:
        logger.info("using existing lago plan", code=lago_plan_code)

    # ── Step 5: Persist to Redis ──
    config_store.save({
        "LAGO_API_URL": lago_base,
        "LAGO_API_HOST": lago_api_host,
        "LAGO_API_KEY": req.lago_api_key,
        "LAGO_WEBHOOK_SECRET": req.lago_webhook_secret,
        "LAGO_PLAN_CODE": lago_plan_code,
        "PADDLE_CLASSIC_URL": req.paddle_classic_url,
        "PADDLE_VENDOR_ID": req.paddle_vendor_id,
        "PADDLE_VENDOR_AUTH_CODE": req.paddle_vendor_auth_code,
        "PADDLE_MONTHLY_PLAN_ID": req.paddle_monthly_plan_id,
        "MIDDLEWARE_URL": middleware_url,
        "PADDLE_PUBLIC_KEY": req.paddle_public_key,
    })
    logger.info("setup complete — config saved to Redis")

    return SetupResponse(
        success=True,
        webhook_url=webhook_url,
        webhook_already_registered=webhook_already_registered,
        lago_plan_code=lago_plan_code,
        paddle_monthly_plan_id=req.paddle_monthly_plan_id,
        message="Setup complete. Config is live immediately — no restart needed.",
    )
