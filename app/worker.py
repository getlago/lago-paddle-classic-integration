from celery import Celery
from app.config import settings

celery_app = Celery(
    "lago_paddle",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.customer_onboarding", "app.tasks.invoice_payment"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)
