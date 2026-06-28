"""UARB scraper (spec §7.6) — the navigate / search / metadata / counts half (Stage 3).

Mechanics are transcribed from the validated spike (``reference/spike.py``), confirmed live
against M12205 headed + headless. Load-bearing facts baked in here:
  - The app paints into the TOP document (the ``UIWidgetSet`` about:blank frame is GWT history) —
    target ``page`` directly, no frame_locator.
  - NEVER use ``networkidle`` (Atmosphere push never idles). Poll for painted ``.fm-textarea`` /
    ``.fm-widget`` with ``.v-loading-indicator`` hidden.
  - Fields are ``div.fm-textarea`` with an editable ``div.text`` — CLICK then ``keyboard.type``;
    ``fill()`` does nothing. Anchor the matter box on its "eg M01234" placeholder.
  - There are ~5 "Search" buttons; pick the matter-box one by GEOMETRY (same row as the field).
  - Counts ride in the type labels ("Other Documents - 42"); one regex pass over body text reads
    all five without opening any tab.

The download loop (``download_type``) and its mandatory guards (Stage 4):
  - Empty-tab "No Matching Records" modal: opening a 0-count tab raises a blocking modal whose
    modality curtain blocks every later click — ``dismiss_modal`` runs before each tab/download.
  - Virtualized list: only ~8 Go-Get-It rows render at once — scroll-collect-dedupe to reach 10.
  - Two-step download: Go Get It → "Download Files" modal → click each file button → expect_download.
  - Per-document retry-then-skip (§9): a failed download retries with backoff, then the doc is
    skipped and counted as failed; a skipped doc never terminates the loop (termination is keyed on
    new-row progress, not download success).
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings
from app.errors import RetryableError
from app.models import DocCounts, DocumentType, DownloadedDoc, MatterMetadata, ScrapeResult
from app.observability import get_logger
from app.scrape import selectors
from app.scrape.browser import launch_browser

log = get_logger(__name__)


class ScrapeStructureError(RetryableError):
    """A required page element could not be located (DOM/selector rot).

    Subclasses ``RetryableError`` so the taxonomy retries-then-alerts per §9 (selector rot is an
    infra/regression signal, NOT a user error). This is deliberately distinct from a clean
    "matter not found", which is signalled by ``search`` returning ``False``.
    """


def parse_counts(body_text: str) -> DocCounts:
    """Pure: extract the five document-type counts from results-screen text (deterministic regex).

    Separated from the page so it is unit-testable offline. A missing label parses as 0 (on a real
    results screen all five labels are always present; the ``search`` results-signal gate guards
    the total-failure case upstream).
    """
    values: dict[str, int] = {}
    for doc_type in selectors.DOC_TYPES:
        m = selectors.count_re(doc_type).search(body_text)
        values[doc_type] = int(m.group(1)) if m else 0
    return DocCounts(
        exhibits=values["Exhibits"],
        key_documents=values["Key Documents"],
        other_documents=values["Other Documents"],
        transcripts=values["Transcripts"],
        recordings=values["Recordings"],
    )


def parse_doc_no(filenames: list[str]) -> str:
    """Derive a stable per-document key from a modal's file list (the first filename's stem).

    One key is used for BOTH the dedupe skip-check and the add (spec §7.6). The row's first file
    name (e.g. "H-1.pdf" -> "H-1") is stable across scroll re-renders.
    """
    if not filenames:
        return ""
    return Path(filenames[0]).stem


async def collect_documents(
    *,
    limit: int,
    list_rows: Callable[[], Awaitable[int]],
    open_modal: Callable[[int], Awaitable[list[str]]],
    download_doc: Callable[[int, list[str], str], Awaitable[DownloadedDoc]],
    close_modal: Callable[[], Awaitable[None]],
    scroll: Callable[[], Awaitable[None]],
    polite_sleep: Callable[[], Awaitable[None]],
    max_no_progress: int = 3,
) -> tuple[list[DownloadedDoc], int]:
    """Pure scroll-collect-dedupe orchestrator (spec §7.6) — no Playwright, so unit-testable.

    Injected callables do the page work; this owns the dedupe set, the termination rule, and the
    retry-then-skip accounting. Termination is keyed on **new-row progress** (a new, non-duplicate
    doc number appeared), NOT on download success — so transient download failures never end
    collection early. Returns ``(documents, failed_count)``.
    """
    done: set[str] = set()
    docs: list[DownloadedDoc] = []
    failed = 0
    no_progress = 0

    while len(docs) < limit and no_progress < max_no_progress:
        k = await list_rows()
        progressed = False
        for i in range(k):
            if len(docs) >= limit:
                break
            try:
                filenames = await open_modal(i)
            except Exception as exc:  # noqa: BLE001 — modal open is transient; try the next row
                log.warning("modal_open_failed", row=i, error=str(exc).splitlines()[0])
                continue
            doc_no = parse_doc_no(filenames)
            if doc_no in done:
                await close_modal()  # scroll overlap — already have this row
                continue
            # A new, non-duplicate row appeared → this counts as progress regardless of whether the
            # download below succeeds. Mark done now so a failing doc is not retried every scroll.
            progressed = True
            done.add(doc_no)
            try:
                doc = await download_doc(i, filenames, doc_no)
                docs.append(doc)
                log.info("doc_downloaded", doc_no=doc_no, files=len(doc.filenames))
            except Exception as exc:  # noqa: BLE001 — retries already exhausted inside download_doc
                failed += 1
                log.warning("doc_download_skipped", doc_no=doc_no, error=str(exc).splitlines()[0])
            await close_modal()
            await polite_sleep()
        if len(docs) >= limit:
            break
        await scroll()
        no_progress = 0 if progressed else no_progress + 1

    return docs, failed


async def download_with_retries(
    doc_no: str,
    n_files: int,
    fetch_file: Callable[[int], Awaitable[tuple[str, str, int]]],
    *,
    attempts: int = 3,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> DownloadedDoc:
    """Pure: download every file of a document, retrying each file's fetch (spec §0.2 + §9).

    ``fetch_file(j)`` performs one file's download and returns ``(saved_name, saved_path, bytes)``;
    it is retried up to ``attempts`` times with exponential backoff on ``retry_exceptions`` only
    (a non-transient bug is not retried — it propagates immediately). After exhaustion the
    exception propagates and the caller skips + counts the whole document. Separated from Playwright
    so the retry + multi-file aggregation is unit-testable offline.
    """
    if n_files == 0:
        raise RetryableError(f"no file buttons in modal for {doc_no}")
    saved_names: list[str] = []
    paths: list[str] = []
    total = 0
    for j in range(n_files):
        name = path = None
        size = 0
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type(retry_exceptions),
            reraise=True,
        ):
            with attempt:
                name, path, size = await fetch_file(j)
        saved_names.append(name)
        paths.append(path)
        total += size
    return DownloadedDoc(doc_no=doc_no, filenames=saved_names, paths=paths, total_bytes=total)


class UARBScraper:
    """Drives one matter's worth of interaction on a single Playwright ``page``."""

    def __init__(self, page: Page, polite_delay_s: float = 0.6) -> None:
        self.page = page
        self.polite_delay_s = polite_delay_s

    # ── Readiness ────────────────────────────────────────────────────────────
    async def goto_and_ready(self) -> float:
        """Navigate and poll until FileMaker widgets are painted and the loader is hidden.

        Returns the seconds waited for readiness. ``domcontentloaded`` + content polling — never
        ``networkidle``.
        """
        self.page.set_default_navigation_timeout(selectors.NAV_TIMEOUT_MS)
        await self.page.goto(selectors.URL, wait_until="domcontentloaded")
        return await self._wait_for_app_ready()

    async def _wait_for_app_ready(self) -> float:
        start = time.monotonic()
        deadline = start + selectors.APP_READY_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            try:
                st = await self.page.evaluate(
                    """() => {
                        const vis = el => { const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0; };
                        const fields = [...document.querySelectorAll('.fm-textarea,.fm-widget')]
                            .filter(vis).length;
                        const li = document.querySelector('.v-loading-indicator');
                        const loading = !!(li && getComputedStyle(li).display !== 'none'
                            && li.getBoundingClientRect().width > 0);
                        return { fields, loading };
                    }"""
                )
            except Exception:
                st = {"fields": 0, "loading": True}
            if st["fields"] > 0 and not st["loading"]:
                waited = time.monotonic() - start
                log.info("app_ready", fields=st["fields"], waited_s=round(waited, 2))
                return waited
            await self.page.wait_for_timeout(200)
        waited = time.monotonic() - start
        log.warning("app_not_ready", waited_s=round(waited, 2))
        return waited

    # ── Search ───────────────────────────────────────────────────────────────
    async def search(self, matter_number: str) -> bool:
        """Type the matter into the placeholder-anchored field, click the same-row Search button.

        Returns ``found``: True if the results signal ("<Type> - <N>") appears, else False
        (matter not found / no results).
        """
        field = self.page.locator(".fm-textarea").filter(
            has=self.page.locator(".placeholder", has_text=selectors.MATTER_PLACEHOLDER)
        )
        n = await field.count()
        if n != 1:
            raise ScrapeStructureError(f"expected 1 matter field, found {n}")

        editable = field.locator(".text")
        await editable.click()
        # FileMaker enters field-edit mode via a server round-trip; typing too early drops chars.
        await self.page.wait_for_timeout(selectors.EDIT_MODE_WAIT_MS)
        await self.page.keyboard.type(matter_number, delay=selectors.TYPE_DELAY_MS)
        await self.page.wait_for_timeout(selectors.POST_TYPE_WAIT_MS)

        # Pick the Search button on the same row as the matter field (geometry); ~5 buttons match.
        # A missing field-box or no candidate button is selector rot (loud + retryable), NOT a
        # clean "matter not found" — the latter is only signalled by an empty results poll below.
        fbox = await field.bounding_box()
        if fbox is None:
            raise ScrapeStructureError("matter field has no bounding box")
        search = self.page.get_by_role("button", name="Search", exact=False)
        ns = await search.count()
        best, best_dy = None, 1e9
        for i in range(ns):
            bb = await search.nth(i).bounding_box()
            if not bb:
                continue
            dy = abs((bb["y"] + bb["height"] / 2) - (fbox["y"] + fbox["height"] / 2))
            if dy < best_dy and bb["x"] > fbox["x"] - 50:
                best, best_dy = i, dy
        if best is None:
            raise ScrapeStructureError(f"no Search button found near matter field (of {ns})")

        await search.nth(best).click()
        found = await self._wait_for_results()
        log.info(
            "search_done",
            matter=matter_number,
            found=found,
            search_buttons=ns,
            chosen_dy_px=round(best_dy, 1),
        )
        return found

    async def _wait_for_results(self) -> bool:
        deadline = time.monotonic() + selectors.RESULTS_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            try:
                ok = await self.page.evaluate(
                    "(types) => types.some(t => (document.body.innerText||'').includes(t + ' -'))",
                    selectors.DOC_TYPES,
                )
            except Exception:
                ok = False
            if ok:
                return True
            await self.page.wait_for_timeout(250)
        return False

    # ── Counts + raw results text ────────────────────────────────────────────
    async def read_counts(self) -> DocCounts:
        """Read all five document-type counts from the results-screen text (deterministic regex).

        No tab opening needed — the counts ride in the type labels. Read live, never hardcoded.
        """
        body = await self._body_text()
        counts = parse_counts(body)
        log.info("counts_read", **counts.model_dump())
        return counts

    async def read_results_text(self) -> str:
        """Return the results-screen ``body.innerText`` for LLM metadata extraction (Stage 5/6).

        Metadata (org/project/amount/type/category/dates) is parsed from this text by the LLM —
        kept as raw text here so the scraper stays deterministic and the LLM owns the fuzzy parse.
        """
        return await self._body_text()

    async def _body_text(self) -> str:
        return await self.page.evaluate("() => document.body.innerText || ''")

    # ── Modal guards (mandatory — spec §7.6) ─────────────────────────────────
    async def wait_curtain_gone(self) -> bool:
        """Poll until the modal modality curtain is gone (event-based, not a fixed sleep)."""
        deadline = time.monotonic() + selectors.CURTAIN_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            if await self.page.locator(".v-window-modalitycurtain").count() == 0:
                return True
            await self.page.wait_for_timeout(80)
        log.warning("curtain_still_present")
        return False

    async def dismiss_modal(self) -> str:
        """Dismiss any open ``.v-window`` modal (e.g. the empty-tab "No Matching Records" dialog).

        Its modality curtain blocks ALL clicks until dismissed — so this MUST run before every
        tab/download action. Returns the dismissed modal's caption, or "" if none was open.
        """
        try:
            win = self.page.locator(".v-window")
            if await win.count() == 0 or not await win.first.is_visible():
                return ""
            caption = ""
            try:
                caption = (
                    await win.first.locator(".v-window-header, .v-window-caption")
                    .first.inner_text()
                ).strip()
            except Exception:
                pass
            for name in ("OK", "Close", "Ok"):
                b = self.page.get_by_role("button", name=name, exact=False)
                if await b.count() > 0 and await b.first.is_visible():
                    await b.first.click()
                    break
            else:
                await self.page.keyboard.press("Escape")
            await self.wait_curtain_gone()
            log.info("modal_dismissed", caption=caption)
            return caption
        except Exception as exc:  # noqa: BLE001 — never let cleanup crash the flow
            log.warning("dismiss_modal_error", error=str(exc).splitlines()[0])
            return ""

    # ── Download mechanics (two-step modal; spec §7.6) ───────────────────────
    async def _scroll_doc_list(self) -> None:
        """Scroll the virtualized Vaadin Grid to render the next rows; wait for them to settle."""
        try:
            grid = self.page.locator(".v-grid").first
            if await grid.count() == 0:
                grid = self.page.locator(".v-panel-content").first
            box = await grid.bounding_box()
            if not box:
                return
            gg = self.page.get_by_role("button", name="Go Get It", exact=False)
            before = await gg.count()
            await self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await self.page.mouse.wheel(0, 520)
            # Wait until the rendered Go-Get-It set stabilises (virtualization re-render finished).
            last, stable = before, 0
            for _ in range(20):
                await self.page.wait_for_timeout(120)
                now = await self.page.get_by_role(
                    "button", name="Go Get It", exact=False
                ).count()
                stable = stable + 1 if now == last else 0
                last = now
                if stable >= 3:
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("scroll_error", error=str(exc).splitlines()[0])

    async def _open_download_modal(self, gg_index: int) -> list[str]:
        """Click a Go-Get-It button and return the file-button labels once the modal is actionable.

        Raises on timeout (the caller treats a failed open as transient and tries the next row).
        """
        gg = self.page.get_by_role("button", name="Go Get It", exact=False)
        await gg.nth(gg_index).click()
        modal = self.page.locator(".v-window")
        await modal.wait_for(state="visible", timeout=selectors.MODAL_TIMEOUT_MS)
        file_btn = modal.get_by_role("button", name=selectors.FILE_BUTTON_RE)
        try:
            await file_btn.first.wait_for(state="visible", timeout=selectors.MODAL_TIMEOUT_MS)
        except PWTimeout:
            # Broad fallback for file types outside the known-extension list (spike behavior).
            file_btn = modal.locator("button", has_text=selectors.FILE_BUTTON_FALLBACK_RE)
            await file_btn.first.wait_for(state="visible", timeout=4_000)
        return [(await file_btn.nth(j).inner_text()).strip() for j in range(await file_btn.count())]

    async def _close_modal(self) -> None:
        """Close the Download Files modal (Close/OK, fallback Escape) and wait for the curtain."""
        try:
            close = self.page.get_by_role("button", name="Close", exact=False)
            if await close.count() > 0 and await close.first.is_visible():
                await close.first.click()
            else:
                await self.page.keyboard.press("Escape")
        except Exception:
            await self.page.keyboard.press("Escape")
        await self.wait_curtain_gone()

    async def _download_doc(
        self, gg_index: int, filenames: list[str], doc_no: str, tmp_dir: Path
    ) -> DownloadedDoc:
        """Download EVERY file button in the open modal (spec §0.2), with per-file retry (§9).

        The modal is already open. Delegates the retry + multi-file aggregation to the pure
        ``download_with_retries`` helper; this method only supplies the Playwright fetch. Each
        file's ``expect_download`` is retried on transient (PWTimeout/OSError) failures only.
        """
        modal = self.page.locator(".v-window")
        file_btn = modal.get_by_role("button", name=selectors.FILE_BUTTON_RE)
        n_files = await file_btn.count()
        if n_files == 0:
            file_btn = modal.locator("button", has_text=selectors.FILE_BUTTON_FALLBACK_RE)
            n_files = await file_btn.count()

        async def _fetch(j: int) -> tuple[str, str, int]:
            async with self.page.expect_download(timeout=selectors.DOWNLOAD_TIMEOUT_MS) as di:
                await file_btn.nth(j).click()
            d = await di.value
            suggested = d.suggested_filename or (
                filenames[j] if j < len(filenames) else f"{doc_no}_{j}.bin"
            )
            # Strip any directory components from the server-controlled name before joining —
            # never let a downloaded filename escape the temp dir.
            safe = Path(suggested).name or f"{doc_no}_{j}.bin"
            dest = tmp_dir / f"{doc_no}__{safe}"
            await d.save_as(str(dest))
            return dest.name, str(dest), dest.stat().st_size

        return await download_with_retries(
            doc_no,
            n_files,
            _fetch,
            retry_exceptions=(PWTimeout, OSError),
        )

    async def _apply_pdf_filter(self) -> None:
        """Best-effort: click the "PDF Only" filter before the loop (spec §7.6).

        Non-PDF types exist in the broader corpus; the filter narrows to expected files. Guarded —
        if the control isn't present (or its label changed) the loop still works (the validated
        spike ran without it), so a miss here is logged, not fatal.
        """
        try:
            btn = self.page.get_by_role("button", name="PDF Only", exact=False)
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click()
                await self.page.wait_for_timeout(400)
                await self.dismiss_modal()
                log.info("pdf_filter_applied")
        except Exception as exc:  # noqa: BLE001
            log.warning("pdf_filter_skipped", error=str(exc).splitlines()[0])

    async def download_type(
        self, document_type: str, limit: int
    ) -> tuple[list[DownloadedDoc], int]:
        """Open the type's tab and download up to ``limit`` distinct documents.

        Returns ``(documents, failed_count)``. Returns ``([], 0)`` for an empty/zero-count tab.
        Dismisses the empty-tab modal before acting, applies the PDF filter, then runs the
        scroll-collect-dedupe loop with per-document retry-then-skip.
        """
        await self.dismiss_modal()  # clear any leftover curtain
        tab = self.page.get_by_role("button", name=document_type, exact=False)
        if await tab.count() == 0:
            log.info("type_tab_absent", document_type=document_type)
            return [], 0
        await tab.first.click()
        await self.dismiss_modal()  # empty-tab "No Matching Records" guard
        await self._apply_pdf_filter()

        gg = self.page.get_by_role("button", name="Go Get It", exact=False)
        try:
            await gg.first.wait_for(state="visible", timeout=selectors.MODAL_TIMEOUT_MS)
        except PWTimeout:
            log.info("no_rows_for_type", document_type=document_type)
            return [], 0

        # Ownership note: the packager (Stage 7) consumes these files and deletes them as it zips.
        # On a mid-scrape failure the dir is left for the OS temp reaper (ephemeral on Cloud Run);
        # the pipeline (Stage 6) is responsible for best-effort cleanup of a partial scrape.
        tmp_dir = Path(tempfile.mkdtemp(prefix="uarb_"))

        async def _list_rows() -> int:
            return await self.page.get_by_role("button", name="Go Get It", exact=False).count()

        async def _download(idx: int, names: list[str], doc_no: str) -> DownloadedDoc:
            return await self._download_doc(idx, names, doc_no, tmp_dir)

        async def _polite_sleep() -> None:
            await self.page.wait_for_timeout(int(self.polite_delay_s * 1000))

        docs, failed = await collect_documents(
            limit=limit,
            list_rows=_list_rows,
            open_modal=self._open_download_modal,
            download_doc=_download,
            close_modal=self._close_modal,
            scroll=self._scroll_doc_list,
            polite_sleep=_polite_sleep,
        )
        log.info(
            "download_type_done",
            document_type=document_type,
            downloaded=len(docs),
            failed=failed,
            tmp_dir=str(tmp_dir),
        )
        return docs, failed


async def scrape_matter(
    matter_number: str,
    document_type: DocumentType,
    *,
    settings: Settings,
    extract_metadata: Callable[[str, str], Awaitable[MatterMetadata]],
) -> ScrapeResult:
    """Full scrape orchestration (spec §4 scrape step): launch → search → counts+metadata → download.

    Owns the browser lifecycle. ``extract_metadata`` is injected (the pipeline passes
    ``llm.extract_metadata``) so this module stays free of any LLM dependency. Counts are read
    deterministically (regex) and are authoritative; the LLM only parses the descriptive metadata
    from the results text. Returns a fully-populated ``ScrapeResult``.
    """
    async with async_playwright() as p:
        browser, context = await launch_browser(p, settings)
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await context.new_page()
        scraper = UARBScraper(page, polite_delay_s=settings.polite_delay_s)
        result: ScrapeResult | None = None
        try:
            await scraper.goto_and_ready()
            found = await scraper.search(matter_number)
            if not found:
                result = ScrapeResult(
                    matter_number=matter_number,
                    found=False,
                    requested_type=document_type,
                )
            else:
                counts = await scraper.read_counts()
                results_text = await scraper.read_results_text()
                metadata = await extract_metadata(matter_number, results_text)
                metadata.counts = counts  # counts are authoritative (regex), not LLM-derived
                type_count = counts.for_type(document_type)
                if type_count == 0:
                    result = ScrapeResult(
                        matter_number=matter_number,
                        found=True,
                        metadata=metadata,
                        requested_type=document_type,
                        type_count=0,
                    )
                else:
                    limit = min(settings.max_documents, type_count)
                    docs, failed = await scraper.download_type(document_type, limit)
                    result = ScrapeResult(
                        matter_number=matter_number,
                        found=True,
                        metadata=metadata,
                        requested_type=document_type,
                        type_count=type_count,
                        documents=docs,
                        requested=limit,
                        downloaded=len(docs),
                        failed=failed,
                    )
            # Success: keep the trace only when configured to (local always; prod on failure only).
            await _finish_trace(context, page, matter_number, settings, failed=False)
            return result
        except Exception:
            # Scrape failure (§8): persist a Playwright trace + screenshot + page content for triage.
            await _finish_trace(context, page, matter_number, settings, failed=True)
            raise
        finally:
            await context.close()
            await browser.close()


async def _finish_trace(context, page, matter_number, settings: Settings, *, failed: bool) -> None:
    """Stop tracing; save trace + screenshot + page content on failure (or always, locally)."""
    if not failed and not settings.trace_always:
        try:
            await context.tracing.stop()  # discard
        except Exception:  # pragma: no cover
            pass
        return
    try:
        art_dir = Path(tempfile.mkdtemp(prefix=f"scrape_{matter_number}_"))
        await context.tracing.stop(path=str(art_dir / "trace.zip"))
        if failed:
            try:
                await page.screenshot(path=str(art_dir / "failure.png"))
                (art_dir / "page.html").write_text(await page.content(), encoding="utf-8")
            except Exception:  # pragma: no cover — best-effort artifacts
                pass
        log.info("scrape_trace_saved", matter=matter_number, dir=str(art_dir), failed=failed)
    except Exception:  # pragma: no cover — never let tracing crash the scrape
        log.warning("scrape_trace_failed", matter=matter_number)
