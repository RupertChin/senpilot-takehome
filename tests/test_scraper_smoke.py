"""Live smoke test of the scraper against M12205 (spec §12 oracle).

Asserts STRUCTURE and that values are read LIVE — never the drifting literal numbers (counts and
amount have already drifted vs the spec oracle per reference/FINDINGS.md). Marked ``live`` (hits
the gov site); run explicitly with ``pytest -m live``. Politeness: one matter, sequential.
"""

from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from app.config import Settings
from app.scrape import selectors
from app.scrape.browser import launch_browser
from app.scrape.scraper import UARBScraper

MATTER = "M12205"

pytestmark = pytest.mark.live


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
