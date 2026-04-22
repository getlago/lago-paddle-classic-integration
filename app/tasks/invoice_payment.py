import asyncio
from app.worker import celery_app
from app.utils.logger import get_logger

logger = get_logger("task.invoice_payment")


@celery_app.task(
    bind=True,
    name="invoice_payment",
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=60,
    acks_late=True,
)
def charge_invoice(self, invoice: dict) -> None:
    from app.flows.invoice_payment import run

    invoice_id   = invoice.get("lago_id", "unknown")
    invoice_type = invoice.get("invoice_type", "unknown")
    logger.info("processing invoice payment", invoice_id=invoice_id, invoice_type=invoice_type)

    try:
        asyncio.run(run(invoice))
    except Exception as exc:
        logger.error("invoice payment failed", invoice_id=invoice_id, error=str(exc))
        raise self.retry(exc=exc)
