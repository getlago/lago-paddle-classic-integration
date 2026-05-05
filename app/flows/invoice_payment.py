"""
Handles invoice.generated webhooks from Lago.

- type: credit      → wallet was funded externally (Paddle already charged); mark invoice paid directly.
- type: subscription → overage invoice; charge the Paddle subscription on file, then mark invoice paid.

Paddle is the Merchant of Record and handles tax automatically. The amount we send
is treated as gross (tax-inclusive) — no gross-up needed on our side.

Idempotency: before charging Paddle, check Redis for a stored order_id for this invoice.
If found, skip the charge and go straight to marking the invoice paid in Lago.
This ensures Paddle is only charged once even if the Celery task retries.
"""
import json
import redis as redis_lib
from app.clients.lago import LagoClient
from app.clients.paddle_classic import PaddleClassicClient, lago_cents_to_paddle_amount
from app.config import settings
from app.utils.config_store import get
from app.utils.logger import get_logger

logger = get_logger("flow.invoice_payment")

_CHARGE_TTL = 60 * 60 * 24 * 7  # 7 days


def _redis():
    return redis_lib.from_url(settings.redis_url, decode_responses=True)


def _charge_key(invoice_id: str) -> str:
    return f"paddle_charge:{invoice_id}"


async def run(invoice: dict) -> None:
    invoice_id   = invoice.get("lago_id")
    invoice_type = invoice.get("invoice_type")
    currency     = invoice.get("currency", "USD")
    amount_cents = invoice.get("fees_amount_cents", 0)

    customer    = invoice.get("customer", {})
    external_id = customer.get("external_id")
    metadata    = {m["key"]: m["value"] for m in customer.get("metadata", [])}

    # Resolve the Paddle subscription for this invoice's plan.
    # Metadata key is paddle_sub_{paddle_plan_id} (e.g. paddle_sub_89290).
    # Look up the paddle_plan_id via the plan map using the invoice's plan code.
    plan_code = None
    for sub in invoice.get("subscriptions", []):
        plan_code = sub.get("plan", {}).get("code")
        if plan_code:
            break

    plan_map = json.loads(get("LAGO_PLAN_MAP") or "[]")
    plan_entry = next((p for p in plan_map if p.get("lago_plan_code") == plan_code), None)
    paddle_plan_id = plan_entry["paddle_plan_id"] if plan_entry else None

    subscription_id = (
        metadata.get(f"paddle_sub_{paddle_plan_id}") if paddle_plan_id else None
    ) or metadata.get("paddle_sub_id")  # fallback for legacy single-plan metadata

    if not subscription_id:
        if invoice_type == "credit":
            # Credit invoice with no plan = wallet was already funded externally (Paddle charge
            # already happened). Just mark it paid in Lago — no Paddle charge needed.
            lago = LagoClient()
            try:
                await lago.mark_invoice_succeeded(invoice_id)
                logger.info("credit invoice marked paid (external top-up)", invoice_id=invoice_id)
            finally:
                await lago.close()
            return
        raise ValueError(f"No paddle_sub found for customer {external_id!r} (plan: {plan_code!r}, paddle_plan_id: {paddle_plan_id!r})")

    lago   = LagoClient()
    paddle = PaddleClassicClient()
    r      = _redis()

    amount = lago_cents_to_paddle_amount(amount_cents, currency)

    try:
        # ── Idempotency: skip charge if Paddle was already charged for this invoice ──
        existing_order_id = r.get(_charge_key(invoice_id))
        if existing_order_id:
            logger.info(
                "paddle already charged for this invoice, skipping charge",
                invoice_id=invoice_id,
                order_id=existing_order_id,
            )
            order_id = existing_order_id
        else:
            logger.info(
                "charging paddle for invoice",
                invoice_id=invoice_id,
                invoice_type=invoice_type,
                amount=amount,
                currency=currency,
                subscription_id=subscription_id,
            )

            result = await paddle.charge_subscription(
                subscription_id=subscription_id,
                amount=amount,
                charge_name=_charge_name(invoice_type, amount_cents, currency),
            )

            if result.get("status") != "success":
                raise RuntimeError(f"Paddle charge failed: {result}")

            order_id = result.get("order_id")
            # Store BEFORE calling Lago so retries skip the charge
            r.set(_charge_key(invoice_id), order_id, ex=_CHARGE_TTL)
            # Tag as middleware-initiated so subscription_payment_succeeded doesn't top up the wallet
            r.set(f"middleware_order:{order_id}", "1", ex=_CHARGE_TTL)
            logger.info("paddle charge succeeded", invoice_id=invoice_id, order_id=order_id)

        # ── Mark Lago invoice as paid ──
        await lago.mark_invoice_succeeded(invoice_id)
        logger.info(
            "invoice payment complete",
            invoice_id=invoice_id,
            order_id=order_id,
            amount=amount,
            currency=currency,
        )

    finally:
        await lago.close()
        await paddle.close()


def _charge_name(invoice_type: str, amount_cents: int, currency: str) -> str:
    amount = lago_cents_to_paddle_amount(amount_cents, currency)
    if invoice_type == "credit":
        return f"Wallet Top-Up — {amount:,.0f} {currency}"[:50]
    return f"AI Token Overage — {amount:,.2f} {currency}"[:50]
