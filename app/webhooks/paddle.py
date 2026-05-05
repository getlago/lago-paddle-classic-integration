import json
import redis as redis_lib
from fastapi import APIRouter, Request
from app.utils.logger import get_logger
from app.clients.lago import LagoClient
from app.utils.config_store import get
from app.config import settings
from app.webhooks.verify.paddle import verify_paddle_signature

router = APIRouter()
logger = get_logger("webhook.paddle")


@router.post("/webhooks/paddle", status_code=200)
async def paddle_webhook(request: Request):
    """
    Receives Paddle Classic webhook events.
    Paddle Classic sends form-encoded POST bodies (not JSON).
    """
    form = await request.form()
    form_data = dict(form)
    verify_paddle_signature(form_data)
    alert_name = form_data.get("alert_name")
    logger.info("paddle webhook received", alert_name=alert_name)

    if alert_name == "subscription_created":
        await _handle_subscription_created(form_data)

    elif alert_name == "subscription_payment_succeeded":
        await _handle_subscription_payment_succeeded(form_data)

    elif alert_name == "subscription_cancelled":
        await _handle_subscription_cancelled(form_data)

    else:
        logger.info("unhandled paddle webhook, ignoring", alert_name=alert_name)

    return {"status": "ok"}


async def _handle_subscription_created(data: dict) -> None:
    """
    Fired when a customer completes the Paddle checkout.

    Two modes:
    - Lago-first: customer already exists in Lago (lago_external_id in passthrough)
      → fetch customer for currency/country, store Paddle IDs, create subscription + wallet
    - Paddle-first: subscription created directly in Paddle (no passthrough)
      → create customer in Lago from Paddle data, then same steps
    """
    subscription_id = data.get("subscription_id")
    user_id         = data.get("user_id")
    user_email      = data.get("email")
    plan_id         = data.get("subscription_plan_id")
    currency        = data.get("currency", "USD")
    country         = data.get("country", "")
    passthrough_raw = data.get("passthrough", "{}")

    try:
        passthrough = json.loads(passthrough_raw)
    except json.JSONDecodeError:
        passthrough = {}

    lago_external_id = passthrough.get("lago_external_id") or str(user_id)

    logger.info(
        "paddle subscription created",
        subscription_id=subscription_id,
        user_id=user_id,
        lago_external_id=lago_external_id,
    )

    lago = LagoClient()
    try:
        existing = await lago.get_customer(lago_external_id)

        if existing is None:
            # Paddle-first: flag this external_id so customer_onboarding skips it
            # (creating the customer fires customer.created back at us)
            r = redis_lib.from_url(settings.redis_url, decode_responses=True)
            r.set(f"paddle_first:{lago_external_id}", "1", ex=300)

            await lago.create_customer(
                external_id=lago_external_id,
                email=user_email,
                currency=currency,
                country=country,
            )
        else:
            # Lago-first: use the currency already configured on the customer
            currency = existing.get("currency") or currency

        # Resolve the Lago plan code from the plan map (falls back to LAGO_PLAN_CODE)
        plan_map_raw = get("LAGO_PLAN_MAP")
        plan_map = json.loads(plan_map_raw) if plan_map_raw else []
        plan_entry = next((p for p in plan_map if str(p.get("paddle_plan_id")) == str(plan_id)), None)
        lago_plan_code = plan_entry["lago_plan_code"] if plan_entry else get("LAGO_PLAN_CODE")

        # Store Paddle IDs in Lago metadata — keyed per plan so multiple subs don't overwrite each other.
        # Key format: paddle_sub_{paddle_plan_id} (e.g. paddle_sub_89290) — stays under Lago's 20-char limit.
        await lago.store_paddle_ids(
            external_id=lago_external_id,
            metadata=[
                {"key": f"paddle_sub_{plan_id}",  "value": str(subscription_id), "display_in_invoice": False},
                {"key": "paddle_user_id",          "value": str(user_id),         "display_in_invoice": False},
                {"key": "paddle_user_email",       "value": str(user_email),      "display_in_invoice": False},
            ],
        )

        # Create Lago subscription — activates billing + usage tracking
        await lago.create_subscription(
            external_customer_id=lago_external_id,
            plan_code=lago_plan_code,
            external_id=f"paddle-sub-{subscription_id}",
            currency=currency,
        )

        # Create prepaid wallet only for pay-as-you-go plans, scoped to the plan's billable metric
        if plan_entry.get("create_wallet", True) if plan_entry else True:
            metric_code = plan_entry.get("billable_metric_code", "ai_tokens") if plan_entry else "ai_tokens"
            await lago.create_wallet(
                external_customer_id=lago_external_id,
                currency=currency,
                billable_metric_code=metric_code,
            )
        else:
            logger.info("skipping wallet creation for entitlement plan", lago_plan_code=lago_plan_code)

        logger.info(
            "customer onboarding complete",
            lago_external_id=lago_external_id,
            subscription_id=subscription_id,
        )

    finally:
        await lago.close()


async def _handle_subscription_cancelled(data: dict) -> None:
    """
    Fired when a Paddle subscription is cancelled.
    Logs the event — billing flows check subscription status before charging.
    """
    subscription_id = data.get("subscription_id")
    passthrough_raw = data.get("passthrough", "{}")

    try:
        passthrough = json.loads(passthrough_raw)
    except json.JSONDecodeError:
        passthrough = {}

    lago_external_id = passthrough.get("lago_external_id", "unknown")

    logger.warning(
        "paddle subscription cancelled — billing blocked",
        subscription_id=subscription_id,
        lago_external_id=lago_external_id,
    )


async def _handle_subscription_payment_succeeded(data: dict) -> None:
    """
    Fired on every successful subscription payment — both renewals and manual charges.

    Only acts when:
    - sale_gross > 0 (skip $0 renewals)
    - order_id is NOT tagged as middleware-initiated (invoice payment charges)
    - customer has an active Lago wallet

    Tops up the customer's Lago wallet with the gross amount paid.
    """
    order_id                = data.get("order_id", "")
    subscription_payment_id = data.get("subscription_payment_id", "")
    # Paddle Classic uses sale_gross for manual charges; amount is None for those
    amount                  = data.get("sale_gross") or data.get("amount", "0")
    currency                = data.get("currency", "USD")

    # Skip $0 renewals
    try:
        if float(amount) <= 0:
            logger.info("zero-amount subscription payment, skipping", order_id=order_id)
            return
    except (ValueError, TypeError):
        return

    # Check it's a wallet-enabled plan
    r = redis_lib.from_url(settings.redis_url, decode_responses=True)

    # Skip if the middleware triggered this charge (invoice payment)
    if r.get(f"middleware_order:{order_id}"):
        logger.info("middleware-initiated charge, skipping wallet top-up", order_id=order_id)
        return

    # Idempotency — skip if already processed
    topup_key = f"topup:{subscription_payment_id}"
    if r.get(topup_key):
        logger.info("wallet top-up already processed, skipping", subscription_payment_id=subscription_payment_id)
        return

    passthrough_raw = data.get("passthrough", "{}")
    try:
        passthrough = json.loads(passthrough_raw)
    except json.JSONDecodeError:
        passthrough = {}
    lago_external_id = passthrough.get("lago_external_id") or str(data.get("user_id"))

    lago = LagoClient()
    try:
        wallet = await lago.get_wallet(lago_external_id)
        if not wallet:
            logger.info("no active wallet for this customer, skipping top-up", lago_external_id=lago_external_id)
            return

        credits = str(float(amount))
        await lago.top_up_wallet(wallet_id=wallet.get("lago_id"), credits=credits)

        # Mark as processed
        r.set(topup_key, "1", ex=60 * 60 * 24 * 7)
        # Flag so invoice_payment knows this credit invoice was pre-funded externally.
        # TTL covers the Celery retry window (default 180s) with margin.
        r.set(f"external_topup:{lago_external_id}", "1", ex=300)

        logger.info(
            "wallet topped up from external charge",
            lago_external_id=lago_external_id,
            amount=amount,
            currency=currency,
            credits=credits,
            order_id=order_id,
        )

    finally:
        await lago.close()
