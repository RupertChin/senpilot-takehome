"""Offline tests for the scraper's deterministic parsing (spec §7.6).

These run in the DEFAULT suite (no `live` marker, no browser) so the core regex + count mapping
are covered in CI without hitting the gov site. Uses five DISTINCT counts so a type->attribute
mapping swap is caught.
"""

from __future__ import annotations

from app.scrape import selectors
from app.scrape.scraper import parse_counts

# A synthetic results-screen text fragment shaped like the live one (counts deliberately distinct).
CANNED = """
Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000
Capital Expenditure Approvals
Water
Awaiting Compliance
Date Received 04/07/2025
Final Filing 10/23/2025
Exhibits - 11
Key Documents - 22
Other Documents - 33
Transcripts - 44
Recordings - 55
"""


def test_parse_counts_maps_each_type_distinctly():
    counts = parse_counts(CANNED)
    # Distinct values pin the type -> DocCounts attribute mapping (a swap would fail here).
    assert counts.exhibits == 11
    assert counts.key_documents == 22
    assert counts.other_documents == 33
    assert counts.transcripts == 44
    assert counts.recordings == 55


def test_parse_counts_missing_label_defaults_zero():
    counts = parse_counts("Exhibits - 7")
    assert counts.exhibits == 7
    assert counts.key_documents == 0
    assert counts.recordings == 0


def test_count_re_tolerates_whitespace_variants():
    assert selectors.count_re("Exhibits").search("Exhibits-9").group(1) == "9"
    assert selectors.count_re("Exhibits").search("Exhibits   -   9").group(1) == "9"
    assert selectors.count_re("Other Documents").search("Other Documents - 42").group(1) == "42"


def test_amount_regex_handles_commas_and_decimals():
    assert selectors.AMOUNT_RE.search("value is $69,275,000 total").group(0) == "$69,275,000"
    assert selectors.AMOUNT_RE.search("$1,234.56").group(0) == "$1,234.56"
    # A bare dollar sign with no digits does not match.
    assert selectors.AMOUNT_RE.search("price: $ TBD") is None


def test_date_regex_is_mmddyyyy_not_month_name():
    dates = selectors.DATE_RE.findall(CANNED)
    assert dates == ["04/07/2025", "10/23/2025"]
    # Month-name format must NOT match (the spike's key gotcha).
    assert selectors.DATE_RE.findall("April 7, 2025") == []


def test_matter_in_body_regex():
    assert selectors.MATTER_IN_BODY_RE.search("matter M12205 here").group(0) == "M12205"


def test_structure_error_is_retryable():
    # Selector rot must retry-then-alert (§9), not be treated as a user error.
    from app.errors import RetryableError, classify_exception
    from app.scrape.scraper import ScrapeStructureError

    err = ScrapeStructureError("missing field")
    assert isinstance(err, RetryableError)
    assert classify_exception(err) is err
