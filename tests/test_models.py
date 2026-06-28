"""Unit tests for data models and validation (spec §6)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import (
    DOCUMENT_TYPES,
    DocCounts,
    MatterMetadata,
    ParsedRequest,
    normalize_matter,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("m12205", "M12205"),
        ("  M12205 ", "M12205"),
        ("m 12205", "M12205"),
        ("M1234", "M1234"),
        ("M123456", "M123456"),
    ],
)
def test_matter_number_normalized_and_accepted(raw, expected):
    pr = ParsedRequest(matter_number=raw)
    assert pr.matter_number == expected


@pytest.mark.parametrize(
    "bad",
    ["M123", "M1234567", "12205", "MX1234", "M-1234", ""],
)
def test_matter_number_invalid_rejected(bad):
    with pytest.raises(ValidationError):
        ParsedRequest(matter_number=bad)


def test_matter_number_none_allowed():
    assert ParsedRequest().matter_number is None


def test_document_type_enum_rejects_unknown():
    with pytest.raises(ValidationError):
        ParsedRequest(document_type="Nonsense")  # type: ignore[arg-type]


def test_document_type_enum_accepts_all_five():
    for dt in DOCUMENT_TYPES:
        assert ParsedRequest(document_type=dt).document_type == dt


def test_normalize_matter_helper():
    assert normalize_matter("  m 12 205 ") == "M12205"


def test_doc_counts_for_type_all_five():
    counts = DocCounts(
        exhibits=13, key_documents=5, other_documents=42, transcripts=7, recordings=3
    )
    # Cover every type so a typo in the _COUNT_ATTR mapping is caught.
    assert counts.for_type("Exhibits") == 13
    assert counts.for_type("Key Documents") == 5
    assert counts.for_type("Other Documents") == 42
    assert counts.for_type("Transcripts") == 7
    assert counts.for_type("Recordings") == 3


def test_matter_metadata_defaults_counts():
    md = MatterMetadata(matter_number="M12205")
    assert md.counts.exhibits == 0
    assert md.organization is None


def test_parsed_request_inherited_flags_default_false():
    pr = ParsedRequest(matter_number="M12205", document_type="Exhibits")
    assert pr.inherited_matter is False
    assert pr.inherited_type is False


def _dt():
    return datetime(2026, 6, 28, tzinfo=timezone.utc)
