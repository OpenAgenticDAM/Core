"""Celery worker: asynchronous ingestion (fast pipeline now, deep pipeline from v0.3)."""

from __future__ import annotations

import uuid

from celery import Celery

from openagenticdam.config import Settings

settings = Settings()
app = Celery("openagenticdam", broker=settings.broker_url, backend=settings.broker_url)
app.conf.update(task_acks_late=True, worker_prefetch_multiplier=1, task_serializer="json", result_expires=3600)


@app.task(name="ingest_object", autoretry_for=(ConnectionError,), retry_backoff=True, max_retries=5)
def ingest_object(tenant_id: str, source_id: str, key: str, acl: list[str]) -> str:
    from openagenticdam.db import connect
    from openagenticdam.ingest import ingest_s3_object

    with connect(settings) as conn:
        asset_id = ingest_s3_object(conn, settings, uuid.UUID(tenant_id), uuid.UUID(source_id), key, acl=acl)
        conn.commit()
    return str(asset_id)
