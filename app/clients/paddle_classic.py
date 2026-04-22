import httpx
from app.utils.config_store import get
from app.utils.logger import get_logger

logger = get_logger("client.paddle_classic")

# Zero-decimal currencies — amount is already in the smallest unit
ZERO_DECIMAL_CURRENCIES = {
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW",
    "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}

# 3-decimal currencies
THREE_DECIMAL_CURRENCIES = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}

# 4-decimal currencies
FOUR_DECIMAL_CURRENCIES = {"CLF"}


def lago_cents_to_paddle_amount(lago_cents: int, currency: str) -> float:
    """Convert Lago integer cents to Paddle currency units."""
    currency = currency.upper()
    if currency in ZERO_DECIMAL_CURRENCIES:
        return float(lago_cents)
    elif currency in THREE_DECIMAL_CURRENCIES:
        return lago_cents / 1_000
    elif currency in FOUR_DECIMAL_CURRENCIES:
        return lago_cents / 10_000
    else:
        return lago_cents / 100


class PaddleClassicClient:
    """
    Client for Paddle Classic API (vendors.paddle.com/api/2.0).
    Auth: vendor_id + vendor_auth_code sent as form fields in every POST.
    """

    def __init__(self):
        self._vendor_id = get("PADDLE_VENDOR_ID")
        self._vendor_auth_code = get("PADDLE_VENDOR_AUTH_CODE")
        self._base_url = get("PADDLE_CLASSIC_URL") or "https://vendors.paddle.com/api/2.0"
        self._client = httpx.AsyncClient(timeout=30.0)

    def _auth(self) -> dict:
        """Base auth fields included in every request body."""
        return {
            "vendor_id": self._vendor_id,
            "vendor_auth_code": self._vendor_auth_code,
        }

    async def generate_pay_link(
        self,
        product_id: str,
        customer_email: str,
        passthrough: str,
    ) -> str:
        """
        Generate a Paddle checkout URL for a given plan.
        passthrough is a JSON string (max 1000 chars) passed back in webhooks.
        Returns the checkout URL.
        """
        resp = await self._client.post(
            f"{self._base_url}/product/generate_pay_link",
            data={
                **self._auth(),
                "product_id": product_id,
                "customer_email": customer_email,
                "passthrough": passthrough,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise ValueError(f"Paddle generate_pay_link failed: {data}")
        url = data["response"]["url"]
        logger.info("paddle checkout link generated", email=customer_email, product_id=product_id)
        return url

    async def charge_subscription(
        self,
        subscription_id: str,
        amount: float,
        charge_name: str,
    ) -> dict:
        """
        One-off charge against an existing subscription (card on file).
        Synchronous — payment succeeds or fails in the API response.
        Returns the full response dict.
        """
        resp = await self._client.post(
            f"{self._base_url}/subscription/{subscription_id}/charge",
            json={
                **self._auth(),
                "amount": amount,
                "charge_name": charge_name,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise ValueError(f"Paddle charge failed: {data}")
        logger.info(
            "paddle subscription charged",
            subscription_id=subscription_id,
            amount=amount,
            order_id=data["response"].get("order_id"),
        )
        return data["response"]

    async def close(self):
        await self._client.aclose()
