import json
import redis as redis_lib
from urllib.parse import quote
from app.models.lago import LagoCustomer
from app.clients.lago import LagoClient
from app.clients.paddle_classic import PaddleClassicClient
from app.utils.config_store import get
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("flow.customer_onboarding")

# Checkout URLs live in Redis for 30 days (long enough to complete checkout)
_CHECKOUT_TTL = 60 * 60 * 24 * 30


def _redis():
    return redis_lib.from_url(settings.redis_url, decode_responses=True)


def checkout_redis_key(external_id: str) -> str:
    return f"checkout_url:{external_id}"


async def run(customer: LagoCustomer) -> None:
    """
    Onboarding flow triggered by customer.created webhook from Lago.

    Steps:
    1. Generate a Paddle checkout link for the monthly plan
    2. Store the full URL in Redis (Lago metadata has a 255-char value limit)
    3. Store a short redirect URL (/checkout/{external_id}) in Lago metadata
    4. Paddle fires subscription_created webhook once customer completes checkout
       → handled by webhooks/paddle.py

    Skipped for Paddle-first customers (subscription already exists in Paddle —
    the paddle_first:{external_id} flag is set by webhooks/paddle.py before
    creating the customer in Lago to suppress this flow).
    """
    r = _redis()
    flag_key = f"paddle_first:{customer.external_id}"
    if r.get(flag_key):
        r.delete(flag_key)
        logger.info("paddle-first customer, skipping onboarding", external_id=customer.external_id)
        return

    lago = LagoClient()
    paddle = PaddleClassicClient()

    try:
        if not customer.email:
            logger.warning("customer has no email, skipping onboarding", external_id=customer.external_id)
            return

        logger.info("generating paddle checkout link", external_id=customer.external_id)

        passthrough = json.dumps({
            "lago_customer_id": customer.lago_id,
            "lago_external_id": customer.external_id,
        })

        checkout_url = await paddle.generate_pay_link(
            product_id=get("PADDLE_MONTHLY_PLAN_ID"),
            customer_email=customer.email,
            passthrough=passthrough,
        )

        # Store full URL in Redis — Lago metadata values are capped at 255 chars
        _redis().set(checkout_redis_key(customer.external_id), checkout_url, ex=_CHECKOUT_TTL)
        logger.info("checkout url cached in redis", external_id=customer.external_id)

        # Store a short redirect URL in Lago metadata instead
        middleware_url = get("MIDDLEWARE_URL") or "http://localhost:3000"
        redirect_url = f"{middleware_url}/checkout/{quote(customer.external_id, safe='')}"

        await lago.store_paddle_ids(
            external_id=customer.external_id,
            metadata=[
                {
                    "key": "paddle_checkout_url",
                    "value": redirect_url,
                    "display_in_invoice": False,
                }
            ],
        )

        logger.info(
            "checkout link stored in lago metadata",
            external_id=customer.external_id,
            redirect_url=redirect_url,
        )

    finally:
        await lago.close()
        await paddle.close()
