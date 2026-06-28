"""Error taxonomy (spec §9).

Two error families drive retry behavior and the ``/process`` HTTP status:

- ``RetryableError`` — timeouts, 5xx from the site, connection resets, a download that didn't
  start, transient LLM/API errors. Retried with backoff (tenacity); at the ``/process`` boundary
  an unresolved one returns 5xx so Cloud Tasks retries.
- ``TerminalError`` — matter-not-found, empty type, invalid/missing request, validation failure.
  No retry; produce the user reply; ``/process`` returns 200.

``classify_exception`` centralizes the mapping for arbitrary exceptions bubbling out of a stage.
"""

from __future__ import annotations

import asyncio


class AgentError(Exception):
    """Base class for taxonomy errors. Carries an optional user-facing hint."""

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message


class RetryableError(AgentError):
    """A transient failure. Retry with backoff; surfaces as 5xx at /process if unresolved."""


class TerminalError(AgentError):
    """A permanent failure (user error or validation). No retry; surfaces as 200 at /process."""


# Substrings that, when found in an arbitrary exception's text, indicate a transient condition.
_RETRYABLE_HINTS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "connection refused",
    "econnreset",
    "temporarily unavailable",
    "503",
    "502",
    "504",
    "overloaded",
    "rate limit",
    "too many requests",
    "429",
    "download",  # "download did not start" / "expect_download" failures are transient
)


def classify_exception(exc: BaseException) -> AgentError:
    """Map an arbitrary exception to the retry taxonomy.

    Already-classified errors pass through unchanged. Playwright timeouts, asyncio timeouts, and
    connection-style errors are retryable. Anything else defaults to retryable as well: an
    unexpected exception is more safely retried (and then surfaced via the failure email after
    attempts are exhausted) than silently treated as a user error. Genuine user errors are raised
    explicitly as ``TerminalError`` by the pipeline, never inferred here.
    """
    if isinstance(exc, (RetryableError, TerminalError)):
        return exc

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return RetryableError(f"timeout: {exc!r}")

    # Playwright raises its own TimeoutError subclass; match by name (rather than importing
    # playwright here, keeping errors.py importable without the browser stack). A timeout-named
    # exception is retryable unconditionally — its message does not always carry a hint word.
    if type(exc).__name__ in ("PlaywrightTimeoutError", "TimeoutError"):
        return RetryableError(f"transient browser timeout: {exc!r}")

    text = str(exc).lower()
    if any(h in text for h in _RETRYABLE_HINTS):
        return RetryableError(f"transient error: {exc!r}")

    if isinstance(exc, (ConnectionError, OSError)):
        return RetryableError(f"connection error: {exc!r}")

    # Default: treat unknown failures as retryable (see docstring).
    return RetryableError(f"unclassified error: {exc!r}")
