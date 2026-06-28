"""Application configuration (spec §3).

Single source of truth for environment-driven behavior. ``Settings`` is read from the
process environment (and a local ``.env`` file). The environment is auto-detected: Cloud Run
injects ``K_SERVICE``, so its presence means ``prod`` unless ``ENV`` is set explicitly.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_env() -> Literal["local", "prod"]:
    """Auto-detect: explicit ``ENV`` wins; else ``prod`` on Cloud Run (``K_SERVICE``), else local."""
    explicit = os.getenv("ENV")
    if explicit in ("local", "prod"):
        return explicit  # type: ignore[return-value]
    return "prod" if os.getenv("K_SERVICE") else "local"


def _default_queue_mode() -> Literal["inline", "tasks"]:
    explicit = os.getenv("QUEUE_MODE")
    if explicit in ("inline", "tasks"):
        return explicit  # type: ignore[return-value]
    return "tasks" if _default_env() == "prod" else "inline"


class Settings(BaseSettings):
    """Typed configuration. Defaults match the spec's tables (§3)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Core ──────────────────────────────────────────────────────────────────
    env: Literal["local", "prod"] = Field(default_factory=_default_env)
    queue_mode: Literal["inline", "tasks"] = Field(default_factory=_default_queue_mode)

    # ── Anthropic (required for the core build) ───────────────────────────────
    anthropic_api_key: str = ""

    # ── AgentMail (deferred to phase 8) ───────────────────────────────────────
    agentmail_api_key: str = ""
    agentmail_inbox: str = ""
    agentmail_webhook_secret: str = ""

    # ── Supabase (deferred to phase 7) ────────────────────────────────────────
    supabase_url: str = ""
    supabase_key: str = ""
    database_url: str = ""

    # ── GCP (deferred to phase 8) ─────────────────────────────────────────────
    gcp_project: str = ""
    gcs_bucket: str = ""
    tasks_queue: str = ""
    tasks_location: str = "us-central1"
    process_url: str = ""
    tasks_invoker_sa: str = ""

    # ── Observability ─────────────────────────────────────────────────────────
    sentry_dsn: str = ""

    # ── Models ────────────────────────────────────────────────────────────────
    classify_model: str = "claude-haiku-4-5"
    extract_model: str = "claude-sonnet-4-6"
    summary_model: str = "claude-sonnet-4-6"

    # ── Tunables ──────────────────────────────────────────────────────────────
    max_documents: int = 10
    attach_threshold_bytes: int = 18_000_000
    signed_url_ttl_hours: int = 72
    polite_delay_s: float = 0.6

    # ── Derived, environment-driven behavior (spec §3 lower table) ────────────
    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def headless(self) -> bool:
        """Headed locally (slow_mo for human watching); headless in prod."""
        return self.is_prod

    @property
    def slow_mo_ms(self) -> int:
        return 0 if self.is_prod else 250

    @property
    def json_logs(self) -> bool:
        """JSON to stdout in prod; pretty console locally."""
        return self.is_prod

    @property
    def trace_always(self) -> bool:
        """Playwright trace always locally; on failure only in prod."""
        return not self.is_prod


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings. Use this everywhere rather than constructing ``Settings``."""
    return Settings()
