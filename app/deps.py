"""Dependency factories — select implementations by environment (spec §3 lower table).

Centralizes the local-vs-prod adapter choice so the rest of the app depends only on the
interfaces. Stage 2 wires the local substitutes (``InMemoryStore``, ``FileEmailClient``); the prod
adapters (``SupabaseStore``, ``AgentMailClient``) and the LLM are added in their later stages.
"""

from __future__ import annotations

from app.config import Settings
from app.email.base import EmailClient
from app.email.file import FileEmailClient
from app.llm.base import LLM
from app.store.base import Store
from app.store.memory_store import InMemoryStore


def build_store(settings: Settings) -> Store:
    """Local → ``InMemoryStore``; prod → ``SupabaseStore`` (Stage 10)."""
    if settings.is_prod:
        from app.store.supabase_store import SupabaseStore  # imported lazily (Stage 10)

        return SupabaseStore(settings)
    return InMemoryStore()


def build_email_client(settings: Settings) -> EmailClient:
    """Local → ``FileEmailClient`` (→ ./outbox/); prod → ``AgentMailClient`` (Stage 11)."""
    if settings.is_prod:
        from app.email.agentmail import AgentMailClient  # imported lazily (Stage 11)

        return AgentMailClient(settings)
    return FileEmailClient()


def build_llm(settings: Settings) -> LLM | None:
    """The Anthropic LLM (Stage 5). Returns ``None`` until that stage lands."""
    try:
        from app.llm.anthropic_client import AnthropicLLM  # imported lazily (Stage 5)
    except ImportError:
        return None
    return AnthropicLLM(settings)
