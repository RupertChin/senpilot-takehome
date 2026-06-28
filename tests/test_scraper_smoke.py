"""Live smoke test of the scraper against M12205 (spec §12 oracle).

Asserts STRUCTURE and that values are read LIVE — never the drifting literal numbers (counts and
amount have already drifted vs the spec oracle per reference/FINDINGS.md). Marked ``live`` (hits
the gov site); run explicitly with ``pytest -m live``. Politeness: one matter, sequential.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from app.config import Settings
from app.scrape import selectors
from app.scrape.browser import launch_browser
from app.scrape.scraper import UARBScraper

MATTER = "M12205"

pytestmark = pytest.mark.live


def _looks_like_a_real_file(path: Path) -> bool:
    """A downloaded file should be non-empty and start with a recognizable magic signature."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    head = path.read_bytes()[:8]
    known_magic = (
        head[:4] == b"%PDF"  # PDF
        or head[:4] in (b"II*\x00", b"MM\x00*")  # TIFF
        or head[:3] == b"\xff\xd8\xff"  # JPEG
        or head[:2] == b"PK"  # ZIP / Office
    )
    # Accept a known signature, or any other corpus file type provided it's a real (non-trivial)
    # download — not a 4-byte stub. Avoids a vacuously-true check while tolerating non-PDF types.
    return known_magic or path.stat().st_size >= 100


@pytest.fixture
def settings(monkeypatch):
    # Force headless so the smoke test runs the deploy-like path even locally.
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("K_SERVICE", raising=False)
    return Settings(_env_file=None)


async def test_m12205_navigate_search_counts_metadata(settings):
    async with async_playwright() as p:
        browser, context = await launch_browser(p, settings)
        try:
            page = await context.new_page()
            scraper = UARBScraper(page)

            waited = await scraper.goto_and_ready()
            assert waited >= 0  # app painted (poll returned)

            found = await scraper.search(MATTER)
            assert found is True, "search did not signal results for M12205"

            # ── Counts: structure, not literals ──────────────────────────────
            counts = await scraper.read_counts()
            # All five fields are populated ints (read live, defaulting to 0 only if truly absent).
            for attr in (
                "exhibits",
                "key_documents",
                "other_documents",
                "transcripts",
                "recordings",
            ):
                val = getattr(counts, attr)
                assert isinstance(val, int) and val >= 0
            # Structurally, M12205 has non-trivial Exhibits / Key / Other (these counts drift, so
            # assert "> 0", not the literal 13/5/42).
            assert counts.exhibits > 0
            assert counts.key_documents > 0
            assert counts.other_documents > 0

            # ── Results text carries the metadata fields (live) ───────────────
            text = await scraper.read_results_text()
            assert MATTER in text
            # Title line carries the amount "{org} - {project} - $amount".
            assert selectors.AMOUNT_RE.search(text), "no dollar amount in results text"
            # Two MM/DD/YYYY dates (initial + final filing).
            dates = selectors.DATE_RE.findall(text)
            assert len(dates) >= 2, f"expected >=2 MM/DD/YYYY dates, got {dates}"
            # Type and Category are distinct fields present on the screen.
            assert "Capital Expenditure" in text
            assert "Water" in text
            # The known title fragment (stable text, not a drifting number).
            assert "Halifax Regional Water Commission" in text
        finally:
            await context.close()
            await browser.close()


async def test_m12205_download_other_documents_and_empty_tab(settings):
    """Spec §12: a 10-document pull from Other Documents yields 10 distinct valid files; an
    empty tab (Recordings/Transcripts, count 0) returns [] with its modal dismissed."""
    async with async_playwright() as p:
        browser, context = await launch_browser(p, settings)
        try:
            page = await context.new_page()
            scraper = UARBScraper(page, polite_delay_s=0.6)
            await scraper.goto_and_ready()
            assert await scraper.search(MATTER) is True
            counts = await scraper.read_counts()

            # ── 10-doc pull from Other Documents (structure, not literal counts) ──
            limit = min(10, counts.other_documents)
            assert limit > 0
            docs, failed = await scraper.download_type("Other Documents", limit)
            assert len(docs) == limit, f"expected {limit} docs, got {len(docs)} (failed={failed})"
            doc_nos = [d.doc_no for d in docs]
            assert len(set(doc_nos)) == len(doc_nos), "duplicate documents collected"
            for d in docs:
                assert d.filenames and d.paths and d.total_bytes > 0
                for pth in d.paths:
                    assert _looks_like_a_real_file(Path(pth)), f"invalid file: {pth}"

            # ── Empty tab returns [] and dismisses the "No Matching Records" modal ──
            empty_type = "Recordings" if counts.recordings == 0 else "Transcripts"
            empty_docs, empty_failed = await scraper.download_type(empty_type, 10)
            assert empty_docs == []
            assert empty_failed == 0
        finally:
            await context.close()
            await browser.close()
