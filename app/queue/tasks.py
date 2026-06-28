"""Job dispatch (spec §7.10).

- ``inline`` (local + first deploy): ``await`` the processing function directly.
- ``tasks`` (prod): create a Cloud Task targeting ``/process`` with an OIDC token. Built in the
  deploy stage (Stage 12); raises here until then so an accidental ``QUEUE_MODE=tasks`` locally
  fails loudly rather than silently dropping the job.
"""

from __future__ import annotations

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

    # tasks mode — implemented in the deploy stage (Cloud Tasks + OIDC).
    raise NotImplementedError(
        "QUEUE_MODE=tasks requires the Cloud Tasks dispatch (Stage 12); use inline locally."
    )
