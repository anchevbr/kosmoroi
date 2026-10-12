"""One Celery Beat instance schedules database-owned work."""

from celery import Celery
from sqlalchemy.orm import sessionmaker

from greek_news.config import get_settings
from greek_news.db import engine_for
from greek_news.jobs import run_once


celery_app = Celery("greek_news", broker=get_settings().redis_url)
celery_app.conf.update(
    broker_connection_retry_on_startup=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    beat_schedule={"scan-durable-jobs": {
        "task": "greek_news.tick", "schedule": 60.0,
    }},
)


@celery_app.task(name="greek_news.tick")
def tick() -> dict[str, int]:
    settings = get_settings()
    factory = sessionmaker(engine_for(settings.database_url), expire_on_commit=False)
    return run_once(factory, settings)