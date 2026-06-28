"""Job orchestration (spec §7.2 / §4).

This module owns ``process_job``, the single entry point that runs an inbound email through the
full pipeline (classify → extract → scrape → package → summarize → reply). Stage 2 ships a minimal
skeleton that loads the job, binds the correlation id, and marks it done; the real stage wiring is
filled in from Stage 6 onward. Keeping the signature stable now lets the webhook + queue wiring be
built and tested before the heavy stages land.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.email.base import EmailClient
from app.llm.base import LLM
from app.observability import get_logger, job_context
from app.store.base import Store

log = get_logger(__name__)


@dataclass
class PipelineDeps:
    """The collaborators ``process_job`` needs, injected so they can be swapped per environment."""

    store: Store
    email: EmailClient
    llm: LLM | None = None  # required from Stage 5; optional while the skeleton stands


async def process_job(message_id: str, *, deps: PipelineDeps) -> None:
    """Run the job for ``message_id`` to a terminal status.

    Skeleton (Stage 2): load the job, bind its id as the correlation id, mark it done. The full
    classify/extract/scrape/package/summarize/reply flow (spec §4) is implemented from Stage 6.
    """
    job = await deps.store.load_job(message_id)
    if job is None:
        log.warning("job_not_found", message_id=message_id)
        return

    with job_context(job.job_id):
        log.info("job_received", message_id=message_id, thread_id=job.inbound.thread_id)

        # TODO(Stage 6): classify → extract → scrape → package → summarize → send_reply.
        await deps.store.set_status(job.job_id, "done")
        log.info("job_done", message_id=message_id)
