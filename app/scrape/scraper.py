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

The download loop (``download_type``) and its mandatory guards are added in Stage 4.
"""

from __future__ import annotations

import time

from playwright.async_api import Page

from app.errors import RetryableError
from app.models import DocCounts
from app.observability import get_logger
from app.scrape import selectors

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


class UARBScraper:
    """Drives one matter's worth of interaction on a single Playwright ``page``."""

    def __init__(self, page: Page) -> None:
        self.page = page

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
