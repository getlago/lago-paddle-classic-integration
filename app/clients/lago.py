import httpx
from typing import Optional
from urllib.parse import quote
from app.utils.config_store import get
from app.utils.logger import get_logger

logger = get_logger("client.lago")


class LagoClient:
    def __init__(self):
        api_key = get("LAGO_API_KEY")
        api_url = get("LAGO_API_URL") or "https://api.getlago.com/api/v1"
        api_host = get("LAGO_API_HOST")

        headers = {"Authorization": f"Bearer {api_key}"}
        if api_host:
            headers["Host"] = api_host

        self._client = httpx.AsyncClient(
            base_url=api_url,
            headers=headers,
            timeout=15.0,
        )

    async def create_customer(
        self,
        external_id: str,
        email: str,
        currency: str = "USD",
        country: str = "",
    ) -> dict:
        """Create or update a Lago customer from Paddle data."""
        payload: dict = {
            "customer": {
                "external_id": external_id,
                "name": email,
                "email": email,
                "currency": currency,
                **({"country": country} if country else {}),
            }
        }
        resp = await self._client.post("/customers", json=payload)
        if resp.is_error:
            logger.error("lago create customer error", status=resp.status_code, body=resp.text)
        resp.raise_for_status()
        customer = resp.json().get("customer", {})
        logger.info("lago customer created", external_id=external_id, email=email)
        return customer

    async def store_paddle_ids(
        self,
        external_id: str,
        metadata: list[dict],
    ) -> None:
        """
        Upsert Lago customer with arbitrary metadata key-value pairs.
        Used to store Paddle IDs (checkout URL, subscription_id, etc.)
        """
        payload = {
            "customer": {
                "external_id": external_id,
                "metadata": metadata,
            }
        }
        resp = await self._client.post("/customers", json=payload)
        if resp.is_error:
            logger.error("lago api error", status=resp.status_code, body=resp.text, payload=payload)
        resp.raise_for_status()
        logger.info("lago metadata updated", external_id=external_id, keys=[m["key"] for m in metadata])

    async def get_customer(self, external_id: str) -> dict | None:
        """
        Fetch a Lago customer by external_id. Returns None if not found.
        Periods are encoded as %2E: Lago's Rails router treats a trailing '.'
        as a format separator (e.g. .json) and misroutes the request otherwise.
        """
        encoded = quote(external_id, safe='').replace('.', '%2E')
        resp = await self._client.get(f"/customers/{encoded}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("customer", {})

    async def create_subscription(
        self,
        external_customer_id: str,
        plan_code: str,
        external_id: Optional[str] = None,
        currency: Optional[str] = None,
    ) -> dict:
        """
        Create a Lago subscription linking a customer to a plan.
        currency overrides the plan's amount_currency so it matches the customer's currency.
        external_id is optional — Lago generates one if not provided.
        """
        payload = {
            "subscription": {
                "external_customer_id": external_customer_id,
                "plan_code": plan_code,
                **({"external_id": external_id} if external_id else {}),
                **({"plan_overrides": {"amount_cents": 0, "amount_currency": currency}} if currency else {}),
            }
        }
        resp = await self._client.post("/subscriptions", json=payload)
        if resp.is_error:
            logger.error("lago subscription error", status=resp.status_code, body=resp.text)
        resp.raise_for_status()
        sub = resp.json().get("subscription", {})
        logger.info(
            "lago subscription created",
            external_customer_id=external_customer_id,
            plan_code=plan_code,
            subscription_id=sub.get("lago_id"),
        )
        return sub

    async def mark_invoice_succeeded(self, invoice_id: str) -> None:
        """Mark a Lago invoice as payment succeeded."""
        resp = await self._client.put(
            f"/invoices/{invoice_id}",
            json={"invoice": {"payment_status": "succeeded"}},
        )
        if resp.is_error:
            logger.error("lago mark invoice error", status=resp.status_code, body=resp.text)
        resp.raise_for_status()
        logger.info("lago invoice marked succeeded", invoice_id=invoice_id)

    async def create_wallet(
        self,
        external_customer_id: str,
        currency: str = "USD",
        rate_amount: str = "1",
    ) -> dict | None:
        """
        Create a prepaid wallet for a customer.
        Idempotent — returns None if the customer already has a wallet.
        rate_amount: how many currency units one credit is worth (default 1 credit = $1).
        """
        payload = {
            "wallet": {
                "name": "AI Tokens Wallet",
                "rate_amount": rate_amount,
                "currency": currency,
                "external_customer_id": external_customer_id,
                "paid_credits": "0",
                "granted_credits": "0",
            }
        }
        resp = await self._client.post("/wallets", json=payload)

        if resp.status_code == 422:
            body = resp.json()
            # Lago returns 422 with "wallet_already_exists" if one exists for this customer
            errors = body.get("error_details", {})
            if any("already_exists" in str(v) for v in errors.values()):
                logger.info("wallet already exists, skipping", external_customer_id=external_customer_id)
                return None
            logger.error("lago wallet creation failed", status=resp.status_code, body=resp.text)
            resp.raise_for_status()

        if resp.is_error:
            logger.error("lago wallet creation failed", status=resp.status_code, body=resp.text)
            resp.raise_for_status()

        wallet = resp.json().get("wallet", {})
        logger.info(
            "lago wallet created",
            external_customer_id=external_customer_id,
            wallet_id=wallet.get("lago_id"),
            currency=currency,
        )
        return wallet

    async def close(self):
        await self._client.aclose()
