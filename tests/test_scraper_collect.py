"""Offline tests for the scroll-collect-dedupe orchestrator (spec §7.6).

Runs in the DEFAULT suite (no browser): proves the dedupe, virtualized-scroll, limit, and
retry-then-skip-without-terminating logic with a fake grid — the parts that would otherwise only
be exercised by the slow live download test.
"""

from __future__ import annotations

import pytest

from app.errors import RetryableError
from app.models import DownloadedDoc
from app.scrape.scraper import _short_error, collect_documents, download_with_retries, parse_doc_no


def test_short_error_keeps_actionability_reason():
    exc = Exception(
        "Locator.click: Timeout 8000ms exceeded.\nCall log:\n"
        "  - element is visible, enabled and stable\n"
        '  - <div class="iwps_header"> subtree intercepts pointer events\n'
        "  - retrying click action"
    )
    out = _short_error(exc)
    assert "Timeout 8000ms exceeded" in out
    assert "intercepts pointer events" in out  # the real reason is preserved
    # No actionability line -> just the headline.
    assert _short_error(Exception("Locator.click: Timeout 8000ms exceeded.")) == (
        "Locator.click: Timeout 8000ms exceeded."
    )


def test_parse_doc_no_uses_first_filename_stem():
    assert parse_doc_no(["H-1.pdf"]) == "H-1"
    assert parse_doc_no(["A-3.pdf", "A-3-appendix.pdf"]) == "A-3"
    assert parse_doc_no([]) == ""


class FakeGrid:
    """Simulates a virtualized list: a sliding window over ``docs`` with overlap on scroll."""

    def __init__(self, docs: list[str], window: int = 8, step: int = 4, fail: set[str] | None = None):
        self.docs = docs
        self.window = window
        self.step = step
        self.fail = fail or set()
        self.start = 0
        self.open_raises_for: set[int] = set()
        self.downloads: list[str] = []

    def _rendered(self) -> list[str]:
        return self.docs[self.start : self.start + self.window]

    async def list_rows(self) -> list[int]:
        # The real page side returns banner-clear indices; the fake's rendered rows are all clickable.
        return list(range(len(self._rendered())))

    async def open_modal(self, i: int) -> list[str]:
        if i in self.open_raises_for:
            raise RuntimeError("modal failed to open")
        doc = self._rendered()[i]
        return [f"{doc}.pdf"]

    async def download_doc(self, i: int, names: list[str], doc_no: str) -> DownloadedDoc:
        if doc_no in self.fail:
            raise RuntimeError(f"download failed for {doc_no}")
        self.downloads.append(doc_no)
        return DownloadedDoc(
            doc_no=doc_no, filenames=names, paths=[f"/tmp/{doc_no}.pdf"], total_bytes=1024
        )

    async def close_modal(self) -> None:
        pass

    async def scroll(self) -> None:
        self.start = min(self.start + self.step, max(0, len(self.docs) - 1))

    async def polite_sleep(self) -> None:
        pass

    async def collect(self, limit: int):
        return await collect_documents(
            limit=limit,
            list_rows=self.list_rows,
            open_modal=self.open_modal,
            download_doc=self.download_doc,
            close_modal=self.close_modal,
            scroll=self.scroll,
            polite_sleep=self.polite_sleep,
        )


async def test_collect_reaches_limit_across_scrolls_no_dupes():
    grid = FakeGrid([f"D{i}" for i in range(20)])  # 20 docs, window 8
    docs, failed = await grid.collect(limit=10)
    assert len(docs) == 10
    assert failed == 0
    doc_nos = [d.doc_no for d in docs]
    assert len(set(doc_nos)) == 10  # zero duplicates despite window overlap


async def test_collect_failed_download_skipped_and_counted_not_terminating():
    # D5 always fails; we must still reach 10 SUCCESSFUL docs and count D5 as failed.
    grid = FakeGrid([f"D{i}" for i in range(20)], fail={"D5"})
    docs, failed = await grid.collect(limit=10)
    assert failed == 1
    assert "D5" not in [d.doc_no for d in docs]
    assert len(docs) == 10  # the failure did not consume a success slot or end the loop
    assert len(set(d.doc_no for d in docs)) == 10


async def test_collect_terminates_when_list_exhausted():
    # Only 6 docs but limit 100: must terminate via no-progress, not hang.
    grid = FakeGrid([f"D{i}" for i in range(6)])
    docs, failed = await grid.collect(limit=100)
    assert len(docs) == 6
    assert failed == 0


async def test_collect_modal_open_failure_skips_row():
    grid = FakeGrid([f"D{i}" for i in range(8)], window=8, step=8)
    grid.open_raises_for = {0}  # first row's modal never opens
    docs, failed = await grid.collect(limit=100)
    # D0 is skipped (modal open failed, not a download failure → not counted as failed).
    assert "D0" not in [d.doc_no for d in docs]
    assert failed == 0
    assert len(docs) == 7


async def test_collect_all_failures_returns_empty_no_hang():
    grid = FakeGrid([f"D{i}" for i in range(6)], fail={f"D{i}" for i in range(6)})
    docs, failed = await grid.collect(limit=100)
    assert docs == []
    assert failed == 6


async def test_collect_only_opens_indices_list_rows_returns():
    # list_rows is the banner-clear filter (_clip_resident_indices on the real page). collect must
    # only open the indices it returns — never a row that was filtered out (the overscan row behind
    # the iwps_header banner that used to fail as modal_open_failed row=0).
    class FilteredGrid(FakeGrid):
        async def list_rows(self) -> list[int]:
            # Drop index 0 every pass: it's the overscan buffer row scrolled up behind the banner.
            return [i for i in range(len(self._rendered())) if i != 0]

    grid = FilteredGrid([f"D{i}" for i in range(20)], window=8, step=4)
    opened: list[int] = []
    orig_open = grid.open_modal

    async def tracking_open(i: int):
        opened.append(i)
        return await orig_open(i)

    grid.open_modal = tracking_open
    docs, failed = await grid.collect(limit=10)
    assert 0 not in opened          # the banner-occluded index is never clicked
    assert len(docs) == 10          # the dropped row is recollected at a clickable index next pass
    assert failed == 0
    assert len(set(d.doc_no for d in docs)) == 10


# ── download_with_retries: the per-file retry + multi-file aggregation ────────


async def test_download_with_retries_retries_then_succeeds():
    calls = {"n": 0}

    async def fetch(j):
        calls["n"] += 1
        if calls["n"] < 3:  # fail twice, succeed on the 3rd attempt
            raise TimeoutError("download did not start")
        return ("H-1.pdf", "/tmp/H-1.pdf", 2048)

    doc = await download_with_retries("H-1", 1, fetch)
    assert calls["n"] == 3  # in-place retry actually happened
    assert doc.filenames == ["H-1.pdf"]
    assert doc.total_bytes == 2048


async def test_download_with_retries_gives_up_after_attempts():
    calls = {"n": 0}

    async def fetch(j):
        calls["n"] += 1
        raise TimeoutError("never starts")

    with pytest.raises(TimeoutError):
        await download_with_retries("H-1", 1, fetch, attempts=3)
    assert calls["n"] == 3  # exactly the attempt budget, then reraise


async def test_download_with_retries_aggregates_multiple_files():
    async def fetch(j):
        return (f"A-3-{j}.pdf", f"/tmp/A-3-{j}.pdf", 100 * (j + 1))

    doc = await download_with_retries("A-3", 3, fetch)
    assert doc.filenames == ["A-3-0.pdf", "A-3-1.pdf", "A-3-2.pdf"]
    assert len(doc.paths) == 3
    assert doc.total_bytes == 100 + 200 + 300  # summed across all files


async def test_download_with_retries_non_transient_fails_fast():
    calls = {"n": 0}

    async def fetch(j):
        calls["n"] += 1
        raise ValueError("coding bug, not transient")

    # Only PWTimeout is retryable here; a ValueError must NOT be retried.
    with pytest.raises(ValueError):
        await download_with_retries(
            "H-1", 1, fetch, retry_exceptions=(TimeoutError,)
        )
    assert calls["n"] == 1  # no wasted retries on a non-transient error


async def test_download_with_retries_empty_modal_raises_retryable():
    async def fetch(j):  # pragma: no cover — never called
        raise AssertionError

    with pytest.raises(RetryableError):
        await download_with_retries("H-1", 0, fetch)
