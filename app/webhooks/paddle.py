import json
import redis as redis_lib
from fastapi import APIRouter, Request
from app.utils.logger import get_logger
from app.clients.lago import LagoClient
from app.utils.config_store import get
from app.config import settings

router = APIRouter()
logger = get_logger("webhook.paddle")


@router.post("/webhooks/paddle", status_code=200)
async def paddle_webhook(request: Request):
    """
    Receives Paddle Classic webhook events.
    Paddle Classic sends form-encoded POST bodies (not JSON).
    """
    form = await request.form()
    alert_name = form.get("alert_name")
    logger.info("paddle webhook received", alert_name=alert_name)

    if alert_name == "subscription_created":
        await _handle_subscription_created(dict(form))

    elif alert_name == "subscription_cancelled":
        await _handle_subscription_cancelled(dict(form))

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
            # Lago-first: use the currency/country already configured on the customer
            currency = existing.get("currency") or currency
            country  = existing.get("country") or country

        # Store Paddle IDs in Lago metadata
        await lago.store_paddle_ids(
            external_id=lago_external_id,
            metadata=[
                {"key": "paddle_sub_id",    "value": str(subscription_id), "display_in_invoice": False},
                {"key": "paddle_user_id",   "value": str(user_id),         "display_in_invoice": False},
                {"key": "paddle_plan_id",   "value": str(plan_id),         "display_in_invoice": False},
                {"key": "paddle_user_email","value": str(user_email),      "display_in_invoice": False},
            ],
        )

        # Create Lago subscription — activates billing + usage tracking
        await lago.create_subscription(
            external_customer_id=lago_external_id,
            plan_code=get("LAGO_PLAN_CODE"),
            external_id=f"paddle-sub-{subscription_id}",
            currency=currency,
        )

        # Create prepaid wallet in the customer's currency
        await lago.create_wallet(external_customer_id=lago_external_id, currency=currency)

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
