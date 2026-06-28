"""Pydantic v2 data models (spec §6).

These are the canonical schemas threaded through the whole pipeline. Validation rules:
``matter_number`` matches ``^M\\d{4,6}$`` (uppercase; input normalized by upper-casing and
stripping spaces). ``document_type`` is one of the five enum values; fuzzy mapping
("other docs" -> "Other Documents") happens in LLM extraction, not here.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

DocumentType = Literal[
    "Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings"
]

#: The five document types in their canonical order (matches the site's tab order).
DOCUMENT_TYPES: tuple[DocumentType, ...] = (
    "Exhibits",
    "Key Documents",
    "Other Documents",
    "Transcripts",
    "Recordings",
)

#: Matter numbers: an uppercase ``M`` followed by 4–6 digits.
MATTER_RE = re.compile(r"^M\d{4,6}$")


def normalize_matter(raw: str) -> str:
    """Upper-case and strip all whitespace from a candidate matter number."""
    return re.sub(r"\s+", "", raw).upper()


class InboundEmail(BaseModel):
    """A parsed inbound email. ``message_id`` is the idempotency key."""

    message_id: str
    thread_id: str | None = None
    from_addr: str
    to_addr: str
    subject: str
    body_text: str
    received_at: datetime


class ParsedRequest(BaseModel):
    """The structured request extracted from an email. Either field may be ``None``."""

    matter_number: str | None = None
    document_type: DocumentType | None = None
    inherited_matter: bool = False
    inherited_type: bool = False

    @field_validator("matter_number")
    @classmethod
    def _validate_matter(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = normalize_matter(v)
        if not MATTER_RE.match(normalized):
            raise ValueError(f"invalid matter number: {v!r}")
        return normalized


class DocCounts(BaseModel):
    """Per-type document (row) counts, as read live from the results screen."""

    exhibits: int = 0
    key_documents: int = 0
    other_documents: int = 0
    transcripts: int = 0
    recordings: int = 0

    def for_type(self, document_type: DocumentType) -> int:
        return getattr(self, _COUNT_ATTR[document_type])


#: Map a DocumentType to its DocCounts attribute name.
_COUNT_ATTR: dict[DocumentType, str] = {
    "Exhibits": "exhibits",
    "Key Documents": "key_documents",
    "Other Documents": "other_documents",
    "Transcripts": "transcripts",
    "Recordings": "recordings",
}


class MatterMetadata(BaseModel):
    """Summary metadata for a matter, read live from the results screen (spec §6).

    Numbers/strings are read from the page — never hardcoded or model-invented.
    """

    matter_number: str
    organization: str | None = None
    project: str | None = None
    amount: str | None = None  # e.g. "$69,275,000" — formatting preserved as a string
    type: str | None = None  # e.g. "Capital Expenditure Approvals"
    category: str | None = None  # e.g. "Water"
    status: str | None = None
    date_initial: str | None = None  # MM/DD/YYYY as shown
    date_final: str | None = None
    counts: DocCounts = Field(default_factory=DocCounts)


class DownloadedDoc(BaseModel):
    """One document (grid row). Contributes all its modal file-buttons to the zip (spec §0.2)."""

    doc_no: str
    filenames: list[str]  # 1+ files per document (row)
    paths: list[str]  # local temp paths
    total_bytes: int


class ScrapeResult(BaseModel):
    """The full result of a scrape: metadata, counts, and the downloaded documents."""

    matter_number: str
    found: bool
    metadata: MatterMetadata | None = None
    requested_type: DocumentType
    type_count: int = 0  # how many docs of the requested type exist
    documents: list[DownloadedDoc] = Field(default_factory=list)
    requested: int = 0  # = min(MAX_DOCUMENTS, type_count)
    downloaded: int = 0  # len(documents) that succeeded
    failed: int = 0


class JobRecord(BaseModel):
    """One job per inbound email. ``job_id`` is also the user-facing reference id."""

    job_id: str
    message_id: str
    status: Literal["processing", "done", "failed"]
    inbound: InboundEmail
    created_at: datetime
    updated_at: datetime


class ThreadContext(BaseModel):
    """Per-conversation context for the follow-up feature, keyed by thread id (spec §7.9)."""

    thread_id: str
    last_matter_number: str | None = None
    last_document_type: DocumentType | None = None
    updated_at: datetime
