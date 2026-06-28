"""Observability: structured logging, a correlation-id contextvar, and Sentry init (spec §8).

- structlog renders JSON to stdout in prod, a pretty console locally.
- A ``correlation_id`` contextvar (= job_id) is bound for the whole job and emitted on every line.
- Sentry is initialized only if a DSN is configured; otherwise it is a no-op.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar

import structlog

from app.config import Settings, get_settings

#: Bound to the job_id for the lifetime of a job; emitted on every log line.
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_configured = False


def _add_correlation_id(_logger, _method, event_dict):
    """structlog processor that injects the current correlation id into each event."""
    cid = correlation_id.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def configure_logging(settings: Settings | None = None) -> None:
    """Configure structlog once. JSON renderer in prod, console renderer locally."""
    global _configured
    if _configured:
        return
    settings = settings or get_settings()

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_correlation_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None):
    """Return a bound structlog logger (configuring logging on first use)."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)


def bind_correlation_id(job_id: str) -> None:
    """Bind a job's correlation id for the current async context.

    Prefer ``job_context`` for scoped binding; this bare setter is kept for callers that bind for
    the remainder of a task that is already isolated per-request.
    """
    correlation_id.set(job_id)


@contextmanager
def job_context(job_id: str):
    """Bind ``job_id`` as the correlation id for the duration of the block, then reset it.

    Resetting on exit prevents a stale job id leaking into later log lines if processing ever runs
    inside a shared/long-lived task rather than a per-request one.
    """
    token = correlation_id.set(job_id)
    try:
        yield
    finally:
        correlation_id.reset(token)


def init_sentry(settings: Settings | None = None) -> None:
    """Initialize Sentry if a DSN is configured. No-op otherwise (and on import failure)."""
    settings = settings or get_settings()
    # Require a real DSN. Guards against an empty value, whitespace, or an inline ``.env`` comment
    # accidentally parsed as the value (a real Sentry DSN is a URL).
    if not settings.sentry_dsn or "://" not in settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.env,
            traces_sample_rate=0.0,
        )
    except Exception:  # pragma: no cover — never let observability setup crash the app
        get_logger(__name__).warning("sentry_init_failed", exc_info=True)
