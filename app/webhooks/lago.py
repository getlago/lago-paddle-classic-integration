import json
from fastapi import APIRouter, Request, HTTPException, Depends
from app.webhooks.verify.lago import verify_lago_signature
from app.tasks.customer_onboarding import onboard_customer
from app.tasks.invoice_payment import charge_invoice
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger("webhook.lago")

# Invoice types that require a Paddle charge
_CHARGEABLE_INVOICE_TYPES = {"credit", "subscription"}


@router.post("/webhooks/lago", status_code=200)
async def lago_webhook(request: Request, raw_body: bytes = Depends(verify_lago_signature)):
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    webhook_type = body.get("webhook_type")
    logger.info("lago webhook received", webhook_type=webhook_type)

    if webhook_type == "customer.created":
        customer_data = body.get("customer", {})
        lago_id = customer_data.get("lago_id", "unknown")
        onboard_customer.apply_async(
            args=[customer_data],
            task_id=f"onboarding-{lago_id}",
        )
        logger.info("customer onboarding job enqueued", lago_id=lago_id)

    elif webhook_type == "invoice.generated":
        invoice      = body.get("invoice", {})
        invoice_id   = invoice.get("lago_id", "unknown")
        invoice_type = invoice.get("invoice_type", "")
        amount_cents = invoice.get("fees_amount_cents", 0)

        if invoice_type not in _CHARGEABLE_INVOICE_TYPES:
            logger.info("non-chargeable invoice type, ignoring", invoice_type=invoice_type, invoice_id=invoice_id)
        elif amount_cents == 0:
            logger.info("zero-amount invoice, skipping", invoice_id=invoice_id)
        else:
            charge_invoice.apply_async(
                args=[invoice],
                task_id=f"invoice-payment-{invoice_id}",
            )
            logger.info(
                "invoice payment job enqueued",
                invoice_id=invoice_id,
                invoice_type=invoice_type,
                amount_cents=amount_cents,
            )

    else:
        logger.info("unhandled webhook type, ignoring", webhook_type=webhook_type)

    return {"status": "ok"}
