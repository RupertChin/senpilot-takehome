"""Unit tests pinning the deterministic reply copy (spec §5)."""

from __future__ import annotations

from app.models import DocCounts
from app.replies import clarification, conversational_ack, empty_type, failure, matter_not_found


def test_clarification_variants():
    assert "I need a matter number (like M12205)" in clarification(True, False)
    assert "Which document type would you like" in clarification(False, True)
    both = clarification(True, True)
    assert "I need a matter number" in both and "Which document type" in both


def test_matter_not_found_copy():
    assert (
        matter_not_found("M12205")
        == "I couldn't find matter M12205 on the UARB portal — could you double-check the number?"
    )


def test_empty_type_copy():
    counts = DocCounts(exhibits=13, key_documents=5, other_documents=42, transcripts=0, recordings=0)
    body = empty_type("M12205", "Transcripts", counts)
    assert body.startswith("Matter M12205 has 0 Transcripts.")
    assert "13 Exhibits, 5 Key Documents, 42 Other Documents, 0 Transcripts, and 0 Recordings" in body
    assert body.endswith("Want a different type?")


def test_failure_copy():
    assert failure("M12205", "job-abc") == (
        "Something went wrong fetching M12205; please try again shortly. Reference: job-abc."
    )
    # Unknown matter falls back gracefully.
    assert "your request" in failure(None, "job-abc")


def test_conversational_ack_copy():
    ack = conversational_ack()
    assert ack.startswith("Happy to help!")
    assert "M12205" in ack
