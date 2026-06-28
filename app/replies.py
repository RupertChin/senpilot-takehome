"""Deterministic user-facing reply copy (spec §5).

Clarification / not-found / empty-type / failure messages are fixed templates (not LLM-composed):
they're terminal-but-friendly and must read identically every time. The success/partial body is
LLM-composed with a deterministic fallback — that lives in ``app/llm/prompts.py``.
"""

from __future__ import annotations

from app.models import DocCounts, DocumentType

_DOC_TYPE_LIST = "Exhibits, Key Documents, Other Documents, Transcripts, or Recordings"


def conversational_ack() -> str:
    """A one-line friendly ack for a clear thanks/greeting (spec §4 — cut-order flex)."""
    return (
        "Happy to help! Whenever you need documents, send me a matter number (like M12205) "
        f"and a document type — {_DOC_TYPE_LIST}."
    )


def clarification(missing_matter: bool, missing_type: bool) -> str:
    """Missing matter and/or document type (§5 — combine when both are missing)."""
    parts: list[str] = []
    if missing_matter:
        parts.append("I can fetch that, but I need a matter number (like M12205). Which matter?")
    if missing_type:
        parts.append(f"Which document type would you like — {_DOC_TYPE_LIST}?")
    return " ".join(parts)


def matter_not_found(matter: str) -> str:
    return (
        f"I couldn't find matter {matter} on the UARB portal — "
        "could you double-check the number?"
    )


def empty_type(matter: str, document_type: DocumentType, counts: DocCounts) -> str:
    reference = (
        f"{counts.exhibits} Exhibits, {counts.key_documents} Key Documents, "
        f"{counts.other_documents} Other Documents, {counts.transcripts} Transcripts, "
        f"and {counts.recordings} Recordings"
    )
    return (
        f"Matter {matter} has 0 {document_type}. For reference it has {reference}. "
        "Want a different type?"
    )


def failure(matter: str | None, job_id: str) -> str:
    target = matter or "your request"
    return (
        f"Something went wrong fetching {target}; please try again shortly. Reference: {job_id}."
    )
