"""Unit tests for config auto-detection and the error taxonomy (spec §3, §9)."""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.errors import (
    RetryableError,
    TerminalError,
    classify_exception,
)


def test_env_defaults_to_local_without_k_service(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    s = Settings(_env_file=None)
    assert s.env == "local"
    assert s.queue_mode == "inline"
    assert s.headless is False
    assert s.slow_mo_ms == 250
    assert s.json_logs is False
    assert s.trace_always is True


def test_env_detects_prod_via_k_service(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("K_SERVICE", "senpilot-agent")
    s = Settings(_env_file=None)
    assert s.env == "prod"
    assert s.queue_mode == "tasks"
    assert s.headless is True
    assert s.slow_mo_ms == 0
    assert s.json_logs is True
    assert s.trace_always is False


def test_explicit_env_overrides_k_service(monkeypatch):
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("K_SERVICE", "senpilot-agent")
    s = Settings(_env_file=None)
    assert s.env == "local"


def test_explicit_prod_env_without_k_service(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("K_SERVICE", raising=False)
    s = Settings(_env_file=None)
    assert s.env == "prod"
    assert s.queue_mode == "tasks"


def test_explicit_queue_mode_override(monkeypatch):
    # An operator can force inline even in prod (the documented first-deploy path).
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("QUEUE_MODE", "inline")
    s = Settings(_env_file=None)
    assert s.queue_mode == "inline"


def test_tunable_defaults_match_spec(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    s = Settings(_env_file=None)
    assert s.max_documents == 10
    assert s.attach_threshold_bytes == 18_000_000
    assert s.signed_url_ttl_hours == 72
    assert s.polite_delay_s == 0.6
    assert s.download_timeout_s == 90
    assert s.classify_model == "claude-haiku-4-5"
    assert s.extract_model == "claude-sonnet-4-6"
    assert s.summary_model == "claude-sonnet-4-6"


def test_download_timeout_env_override_threads_to_scraper(monkeypatch):
    monkeypatch.setenv("DOWNLOAD_TIMEOUT_S", "30")
    s = Settings(_env_file=None)
    assert s.download_timeout_s == 30
    from app.scrape.scraper import UARBScraper

    scraper = UARBScraper(page=None, download_timeout_s=s.download_timeout_s)
    assert scraper.download_timeout_ms == 30_000  # seconds -> ms, env-driven


# ── Error taxonomy ────────────────────────────────────────────────────────────


def test_already_classified_pass_through():
    r = RetryableError("x")
    t = TerminalError("y")
    assert classify_exception(r) is r
    assert classify_exception(t) is t


def test_asyncio_timeout_is_retryable():
    assert isinstance(classify_exception(asyncio.TimeoutError()), RetryableError)


def test_builtin_timeout_is_retryable():
    assert isinstance(classify_exception(TimeoutError("boom")), RetryableError)


def test_connection_error_is_retryable():
    assert isinstance(classify_exception(ConnectionResetError("reset")), RetryableError)


@pytest.mark.parametrize(
    "msg",
    ["503 Service Unavailable", "rate limit exceeded", "download did not start", "overloaded"],
)
def test_transient_text_is_retryable(msg):
    assert isinstance(classify_exception(RuntimeError(msg)), RetryableError)


def test_unknown_exception_defaults_retryable():
    # Unknown failures are safer to retry then surface via the failure email than to swallow.
    assert isinstance(classify_exception(ValueError("weird")), RetryableError)


def test_playwright_timeout_name_branch_retryable():
    # A Playwright timeout (matched by class name, no hint word in the message) is retryable.
    class PlaywrightTimeoutError(Exception):
        pass

    exc = PlaywrightTimeoutError("Timeout 30000ms exceeded")
    assert isinstance(classify_exception(exc), RetryableError)


def test_terminal_carries_user_message():
    t = TerminalError("not found", user_message="I couldn't find that matter.")
    assert t.user_message == "I couldn't find that matter."
