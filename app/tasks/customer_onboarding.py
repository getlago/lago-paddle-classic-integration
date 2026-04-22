import asyncio
from app.worker import celery_app
from app.models.lago import LagoCustomer
from app.utils.logger import get_logger

logger = get_logger("task.customer_onboarding")


@celery_app.task(
    bind=True,
    name="customer_onboarding",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=60,
    acks_late=True,
)
def onboard_customer(self, payload: dict) -> None:
    from app.flows.customer_onboarding import run

    customer = LagoCustomer(**payload)
    logger.info("onboarding customer", external_id=customer.external_id)

    try:
        asyncio.run(run(customer))
    except Exception as exc:
        logger.error("onboarding failed", error=str(exc), external_id=customer.external_id)
        raise self.retry(exc=exc)
