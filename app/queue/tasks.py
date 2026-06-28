"""Job dispatch (spec §7.10).

- ``inline`` (local + first deploy): ``await`` the processing function directly.
- ``tasks`` (prod): create a Cloud Task POSTing ``{message_id}`` to ``/process`` with an OIDC token
  minted for ``tasks_invoker_sa`` (audience = ``process_url``). ``/process`` returns 5xx only for
  retryable failures so Cloud Tasks retries those but not user errors.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from app.config import Settings
from app.observability import get_logger

log = get_logger(__name__)

#: A processing function: takes a message_id and runs the job to completion.
ProcessFn = Callable[[str], Awaitable[None]]


async def enqueue(message_id: str, *, settings: Settings, process_fn: ProcessFn) -> None:
    """Dispatch a claimed message for processing according to ``queue_mode``."""
    if settings.queue_mode == "inline":
        log.info("dispatch_inline", message_id=message_id)
        await process_fn(message_id)
        return

    import asyncio

    await asyncio.to_thread(_create_cloud_task, message_id, settings)
    log.info("dispatch_task_enqueued", message_id=message_id)


def _create_cloud_task(message_id: str, settings: Settings) -> None:
    """Create a Cloud Task targeting ``/process`` with an OIDC token (sync — runs in a thread)."""
    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        settings.gcp_project, settings.tasks_location, settings.tasks_queue
    )
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": settings.process_url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message_id": message_id}).encode(),
            "oidc_token": {
                "service_account_email": settings.tasks_invoker_sa,
                "audience": settings.process_url,
            },
        }
    }
    client.create_task(parent=parent, task=task)
