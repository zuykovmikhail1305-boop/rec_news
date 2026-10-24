"""RabbitMQ transport; no Celery result backend: query results through the API."""
import os

from celery import Celery
from celery.signals import worker_process_init
from kombu import Exchange, Queue

from .database import engine

exchange = Exchange("rec_news.parser", type="direct", durable=True)
celery_app = Celery("rec_news.parser", broker=os.getenv(
    "PARSER_BROKER_URL", "amqp://guest:guest@127.0.0.1:5672//"),
    include=["parser_app.tasks"])
celery_app.conf.update(
    task_queues=(Queue("parser.publications", exchange, routing_key="parser.publications", durable=True),
                 Queue("parser.dispatch", exchange, routing_key="parser.dispatch", durable=True)),
    task_routes={"parser.collect": {"queue": "parser.publications", "routing_key": "parser.publications"},
                 "parser.dispatch_pending": {"queue": "parser.dispatch", "routing_key": "parser.dispatch"}},
    task_default_queue="parser.publications", task_default_delivery_mode="persistent",
    task_create_missing_queues=False,
    task_serializer="json", accept_content=["json"], result_serializer="json",
    task_ignore_result=True, task_store_errors_even_if_ignored=False,
    task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # RabbitMQ 4.3+ rejects transient non-exclusive service queues.
    # Control/reply and event queues are private to each live connection.
    control_queue_exclusive=True, event_queue_exclusive=True,
    broker_connection_retry_on_startup=True, broker_connection_timeout=3,
    broker_transport_options={"confirm_publish": True},
    task_publish_retry=False, broker_heartbeat=30,
    timezone="UTC", enable_utc=True,
    beat_schedule={"parser-reconcile-db": {
        "task": "parser.dispatch_pending", "schedule": 10.0,
        "options": {"queue": "parser.dispatch", "routing_key": "parser.dispatch", "expires": 30},
    }},
)


@worker_process_init.connect
def reset_pool(**kwargs):
    # Prefork children must not share database connections with the parent.
    engine.dispose(close=False)
