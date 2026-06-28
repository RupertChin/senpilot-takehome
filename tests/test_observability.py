"""Smoke tests for observability setup (spec §8): nothing here should throw."""

from __future__ import annotations

from app.config import Settings
from app.observability import (
    bind_correlation_id,
    configure_logging,
    correlation_id,
    get_logger,
    init_sentry,
)


def test_configure_and_log_do_not_throw():
    configure_logging(Settings(_env_file=None))
    log = get_logger("test")
    log.info("hello", k=1)  # must not raise


def test_bind_correlation_id_sets_contextvar():
    bind_correlation_id("job-123")
    assert correlation_id.get() == "job-123"


def test_init_sentry_noop_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    s = Settings(_env_file=None)
    assert s.sentry_dsn == ""
    init_sentry(s)  # no DSN -> no-op, must not raise


def test_json_logs_in_prod(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("K_SERVICE", raising=False)
    s = Settings(_env_file=None)
    assert s.json_logs is True
