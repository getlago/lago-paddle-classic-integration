import redis as redis_lib
from urllib.parse import quote
from app.models.lago import LagoCustomer
from app.clients.lago import LagoClient
from app.utils.config_store import get
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("flow.customer_onboarding")

# Cached data lives for 30 days — enough time to complete checkout
_CHECKOUT_TTL = 60 * 60 * 24 * 30


def _redis():
    return redis_lib.from_url(settings.redis_url, decode_responses=True)


def checkout_email_key(external_id: str) -> str:
    return f"checkout_email:{external_id}"


async def run(customer: LagoCustomer) -> None:
    """
    Onboarding flow triggered by customer.created webhook from Lago.

    Steps:
    1. Cache the customer email in Redis (used to pre-fill Paddle checkout)
    2. Store the /checkout/{external_id} link in Lago metadata
    3. Customer visits the link → middleware shows plan picker (or redirects directly
       for single-plan setups) → Paddle checkout → subscription_created webhook fires
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
    try:
        if not customer.email:
            logger.warning("customer has no email, skipping onboarding", external_id=customer.external_id)
            return

        # Cache email — /checkout uses it to pre-fill the Paddle checkout form
        r.set(checkout_email_key(customer.external_id), customer.email, ex=_CHECKOUT_TTL)

        middleware_url = get("MIDDLEWARE_URL") or "http://localhost:3000"
        redirect_url = f"{middleware_url}/checkout/{quote(customer.external_id, safe='')}"

        await lago.store_paddle_ids(
            external_id=customer.external_id,
            metadata=[{
                "key": "paddle_checkout_url",
                "value": redirect_url,
                "display_in_invoice": False,
            }],
        )

        logger.info(
            "checkout link stored in lago metadata",
            external_id=customer.external_id,
            redirect_url=redirect_url,
        )

    finally:
        await lago.close()
