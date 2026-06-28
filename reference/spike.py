"""
Scrape Spike — Nova Scotia UARB (FileMaker WebDirect / Vaadin 8) discovery script.

THROWAWAY investigative spike. Goal: learn how the site behaves and capture evidence
(selectors, timing, download mechanism, gotchas) so a reliable scraper can be written later.
Optimize for understanding + evidence, NOT clean code. One linear script.

Target: https://uarb.novascotia.ca/fmi/webd/UARB15  (FileMaker WebDirect on Vaadin 8)
Test matter: M12205

VALIDATED against the live site (selectors below are confirmed working, not guesses):
- The app paints into the TOP document (a 2nd frame `...UIWidgetSet` is about:blank GWT history,
  NOT content). `networkidle` is unreliable (Atmosphere push); use domcontentloaded + poll on
  `.fm-textarea/.fm-widget` with the `.v-loading-indicator` hidden.
- FileMaker renders fields as `div.fm-textarea` (NOT <input>) with an editable child `div.text`
  and a `.placeholder`. The matter box = the `.fm-textarea` whose `.placeholder` is "eg M01234".
  You CLICK `.text` then keyboard.type (fill() does not work).
- Buttons are real <button> (accessible name works). There are 3 "Search" buttons; the matter-box
  one is selected by GEOMETRY (same row as the matter field).
- Document-type "tabs" are <button>s whose label embeds the count: "Exhibits - 13",
  "Other Documents - 42", etc. All five counts are readable on the results screen.
- Download = TWO STEP, path (a): click "Go Get It" on a row -> a `.v-window` modal "Download Files"
  appears with a `<docno>.pdf` button -> clicking it fires a real browser download
  (page.expect_download) from `/fmi/webd/APP/connector/<n>/<n>/dl/<file>`
  (content-disposition: attachment, application/octet-stream). Files are real PDFs.

Every step is wrapped so a failure still captures artifacts and the run continues.
"""

import argparse
import asyncio
import json
import pathlib
import re
import time
import traceback
from datetime import datetime, timezone

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------------------
URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
MATTER = "M12205"
SLOW_MO = 250
NAV_TIMEOUT = 60_000
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DOC_TYPES = ["Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings"]
MATTER_PLACEHOLDER = "M01234"   # the 'eg M01234' prompt that uniquely identifies the matter box

# Download tuning. Waits below are EVENT-BASED (wait for the actual condition) rather than fixed
# sleeps, so they adapt to server speed — faster on average AND more robust under load. The only
# fixed pause is POLITE_DELAY_S, a deliberate courtesy throttle between downloads on a gov site.
POLITE_DELAY_S = 0.6            # intentional gap between downloads (politeness, not robustness)
DOWNLOAD_TIMEOUT_MS = 90_000   # generous: one Exhibit was 48 MB; don't time out large transfers
CURTAIN_TIMEOUT_MS = 8_000     # max wait for the modality curtain to clear after closing a modal
MODAL_TIMEOUT_MS = 12_000      # max wait for the Download Files modal + its file button

ART = pathlib.Path("spike-artifacts"); ART.mkdir(exist_ok=True)
DL_DIR = ART / "downloads"; DL_DIR.mkdir(exist_ok=True)
RUN_LOG = ART / "run.log"
NOTES = {}
_t0 = time.monotonic()


def log(msg: str) -> None:
    line = f"[{time.monotonic() - _t0:7.2f}s] {msg}"
    print(line, flush=True)
    with RUN_LOG.open("a") as f:
        f.write(line + "\n")


def note(key, value):
    NOTES[key] = value
    log(f"NOTE  {key} = {value!r}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------------------
class NetRecorder:
    def __init__(self):
        self.active = False
        self.events = []

    def on_request(self, req):
        if self.active:
            self.events.append({"phase": "request", "ts": now_iso(), "method": req.method,
                                "url": req.url, "resource_type": req.resource_type})

    async def on_response(self, resp):
        if not self.active:
            return
        try:
            h = await resp.all_headers()
        except Exception:
            h = {}
        self.events.append({"phase": "response", "ts": now_iso(), "status": resp.status,
                            "url": resp.url, "content_type": h.get("content-type", ""),
                            "content_disposition": h.get("content-disposition", ""),
                            "content_length": h.get("content-length", "")})


# --------------------------------------------------------------------------------------
# Readiness + artifact helpers
# --------------------------------------------------------------------------------------
async def wait_for_app_ready(page, timeout_ms=30_000) -> float:
    """Poll until FileMaker widgets are painted and the Vaadin loading indicator is hidden."""
    start = time.monotonic()
    deadline = start + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            st = await page.evaluate(
                """() => {
                    const vis = el => { const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0; };
                    const fields = [...document.querySelectorAll('.fm-textarea,.fm-widget')]
                        .filter(vis).length;
                    const li = document.querySelector('.v-loading-indicator');
                    const loading = !!(li && getComputedStyle(li).display !== 'none'
                        && li.getBoundingClientRect().width > 0);
                    return { fields, loading };
                }""")
        except Exception:
            st = {"fields": 0, "loading": True}
        if st["fields"] > 0 and not st["loading"]:
            waited = time.monotonic() - start
            log(f"app ready: {st['fields']} fm widgets after {waited:.2f}s")
            return waited
        await page.wait_for_timeout(200)
    log(f"WARN app not ready after {time.monotonic() - start:.2f}s")
    return time.monotonic() - start


async def dump_html(scope, name):
    try:
        html = await scope.content()
        (ART / name).write_text(html, encoding="utf-8")
        log(f"dumped HTML -> {name} ({len(html)} bytes)")
    except Exception as e:
        log(f"WARN dump_html({name}) failed: {e}")


async def shot(page, name):
    try:
        await page.screenshot(path=str(ART / name))
        log(f"screenshot -> {name}")
    except Exception as e:
        log(f"WARN screenshot({name}) failed: {e}")


async def widget_inventory(scope, label):
    try:
        inv = await scope.evaluate(
            r"""() => {
                const txt = el => (el.innerText||el.textContent||'').trim().slice(0,120);
                const vis = el => { const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0; };
                const buttons = [...document.querySelectorAll('button,.v-button,[role=button]')]
                    .filter(vis).map(b => ({ text: txt(b), aria: b.getAttribute('aria-label')||'',
                        cls: b.className }));
                const fields = [...document.querySelectorAll('.fm-textarea,.fm-datefield,.v-filterselect')]
                    .filter(vis).map(f => ({ cls: f.className,
                        placeholder: (f.querySelector('.placeholder')||{}).textContent||'' }));
                const captions = [...document.querySelectorAll('.v-caption,.v-captiontext,.v-label')]
                    .filter(vis).map(txt).filter(Boolean).slice(0,120);
                return { buttons, fields, captions };
            }""")
    except Exception as e:
        log(f"WARN widget_inventory failed: {e}")
        inv = {"error": str(e)}
    (ART / f"inventory_{label}.json").write_text(json.dumps(inv, indent=2))
    log(f"inventory [{label}]: {len(inv.get('buttons', []))} buttons, "
        f"{len(inv.get('fields', []))} fields, {len(inv.get('captions', []))} captions")
    return inv


async def a11y_snapshot(page, name):
    """Accessibility tree. page.accessibility was removed in recent Playwright; use the
    locator-based aria_snapshot (YAML)."""
    try:
        tree = await page.locator("body").aria_snapshot()
        (ART / name).write_text(tree)
        log(f"a11y snapshot -> {name}")
    except Exception as e:
        log(f"WARN a11y_snapshot failed: {e}")


async def wait_curtain_gone(page, timeout_ms=CURTAIN_TIMEOUT_MS) -> bool:
    """Event-based wait: poll until the modal modality curtain is gone (modal fully closed).
    Returns True if cleared, False on timeout. Adapts to server speed instead of a fixed sleep."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if await page.locator(".v-window-modalitycurtain").count() == 0:
            return True
        await page.wait_for_timeout(80)
    log("WARN modality curtain still present after timeout")
    return False


async def dismiss_modal(page) -> str:
    """FileMaker pops a '.v-window' modal (e.g. 'No Matching Records') whose modality curtain
    blocks all clicks. Dismiss it via OK/Close (fallback Escape) and wait for the curtain to go.
    Returns the modal caption if one was dismissed, else ''."""
    try:
        win = page.locator(".v-window")
        if await win.count() == 0 or not await win.first.is_visible():
            return ""
        caption = ""
        try:
            caption = (await win.first.locator(".v-window-header, .v-window-caption")
                       .first.inner_text()).strip()
        except Exception:
            pass
        for name in ("OK", "Close", "Ok"):
            b = page.get_by_role("button", name=name, exact=False)
            if await b.count() > 0 and await b.first.is_visible():
                await b.first.click()
                break
        else:
            await page.keyboard.press("Escape")
        await wait_curtain_gone(page)
        log(f"dismissed modal: {caption!r}")
        return caption
    except Exception as e:
        log(f"WARN dismiss_modal: {e}")
        return ""


def describe_frames(page):
    frames = [{"name": fr.name, "url": fr.url} for fr in page.frames]
    log(f"frames ({len(frames)}): " + json.dumps(frames))
    return frames


# --------------------------------------------------------------------------------------
# Interaction (validated)
# --------------------------------------------------------------------------------------
async def enter_matter_and_search(page, matter) -> dict:
    """Type the matter into the fm-textarea identified by its 'eg M01234' placeholder, then
    click the Search button on the same row (geometry). Returns timing/locator evidence."""
    info = {}
    field = page.locator(".fm-textarea").filter(
        has=page.locator(".placeholder", has_text=MATTER_PLACEHOLDER))
    n = await field.count()
    info["matter_field_count"] = n
    info["matter_locator"] = ".fm-textarea:has(.placeholder:has-text('M01234')) >> .text"
    if n != 1:
        raise RuntimeError(f"expected 1 matter field, found {n}")
    editable = field.locator(".text")
    await editable.click()
    await page.wait_for_timeout(700)         # FileMaker enters field-edit mode (server round-trip)
    await page.keyboard.type(matter, delay=60)
    await page.wait_for_timeout(400)
    info["typed_value"] = (await editable.inner_text()).strip()

    fbox = await field.bounding_box()
    search = page.get_by_role("button", name="Search", exact=False)
    ns = await search.count()
    best, best_dy = None, 1e9
    for i in range(ns):
        bb = await search.nth(i).bounding_box()
        if not bb:
            continue
        dy = abs((bb["y"] + bb["height"] / 2) - (fbox["y"] + fbox["height"] / 2))
        if dy < best_dy and bb["x"] > fbox["x"] - 50:
            best, best_dy = i, dy
    info["search_buttons_total"] = ns
    info["search_chosen_index"] = best
    info["search_row_dy_px"] = round(best_dy, 1)
    info["search_locator"] = "get_by_role('button', name='Search') nearest matter-field row"

    url_before = page.url
    t = time.monotonic()
    await search.nth(best).click()
    signalled = await wait_for_results(page)
    info["search_seconds"] = round(time.monotonic() - t, 2)
    info["search_signal_found"] = signalled
    info["search_navigation"] = "full-nav" if page.url != url_before else "in-place-AJAX(same URL)"
    return info


async def wait_for_results(page, timeout_ms=30_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            ok = await page.evaluate(
                "(types) => types.some(t => (document.body.innerText||'').includes(t + ' -'))",
                DOC_TYPES)
        except Exception:
            ok = False
        if ok:
            return True
        await page.wait_for_timeout(250)
    return False


# --------------------------------------------------------------------------------------
# Metadata + counts (from the results screen text; values confirmed against oracle)
# --------------------------------------------------------------------------------------
async def scrape_metadata_and_counts(page) -> dict:
    data = await page.evaluate(
        r"""() => {
            const vis = el => { const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0; };
            const labels = [...document.querySelectorAll('.v-label,.v-caption,.v-captiontext')]
                .filter(vis).map(e => (e.innerText||e.textContent||'').trim()).filter(Boolean);
            return { body: document.body.innerText || '', labels };
        }""")
    body, labels = data["body"], data["labels"]
    (ART / "results_text.txt").write_text(body, encoding="utf-8")
    (ART / "results_labels.json").write_text(json.dumps(labels, indent=2))

    meta = {}
    # Title line carries the amount: "<Org> - <Project> - $<amount>"
    title_line = next((l for l in body.splitlines()
                       if (" - $" in l) or ("Commission" in l and "Project" in l)), None)
    meta["title"] = title_line
    am = re.search(r"\$[\d,]+(?:\.\d+)?", body)
    meta["amount"] = am.group(0) if am else None
    dates = re.findall(r"\b\d{2}/\d{2}/\d{4}\b", body)
    meta["date_received_initial"] = dates[0] if len(dates) >= 1 else None
    meta["date_final"] = dates[1] if len(dates) >= 2 else None
    meta["matter_no"] = (re.search(r"\bM\d{5}\b", body) or [None])[0] if re.search(r"\bM\d{5}\b", body) else None
    # Type / Category / Status are short standalone label lines near the top
    for key, kw in [("type", "Capital Expenditure"), ("category", "Water"),
                    ("status", "Awaiting Compliance")]:
        meta[key] = kw if kw in body else None
    meta["status"] = next((l.strip() for l in body.splitlines()
                           if l.strip() in ("Awaiting Compliance", "Closed", "Active")), meta.get("status"))

    counts = {t: None for t in DOC_TYPES}
    for t in DOC_TYPES:
        m = re.search(rf"{re.escape(t)}\s*-\s*(\d+)", body)
        if m:
            counts[t] = int(m.group(1))
    return {"metadata": meta, "counts": counts, "n_labels": len(labels)}


async def survey_tabs(page, tag) -> list:
    """Open each of the five document-type tab buttons; screenshot; record count + Go-Get-It."""
    out = []
    for label in DOC_TYPES:
        rec = {"type": label, "count": None, "go_get_it": 0, "opened": False, "modal": ""}
        await dismiss_modal(page)                       # clear any leftover curtain first
        btn = page.get_by_role("button", name=label, exact=False)
        try:
            if await btn.count() > 0:
                await btn.first.click(timeout=8000)
                await page.wait_for_timeout(1200)
                rec["opened"] = True
        except Exception as e:
            log(f"WARN open tab '{label}': {str(e).splitlines()[0]}")
        await shot(page, f"{tag}_tab_{label.replace(' ', '_')}.png")
        # empty tabs raise a 'No Matching Records' modal — record + dismiss it
        rec["modal"] = await dismiss_modal(page)
        try:
            body = await page.evaluate("() => document.body.innerText || ''")
            m = re.search(rf"{re.escape(label)}\s*-\s*(\d+)", body)
            rec["count"] = int(m.group(1)) if m else None
            rec["go_get_it"] = await page.get_by_role("button", name="Go Get It",
                                                      exact=False).count()
        except Exception:
            pass
        log(f"tab '{label}': count={rec['count']} go_get_it={rec['go_get_it']} "
            f"opened={rec['opened']} modal={rec['modal']!r}")
        out.append(rec)
    await dismiss_modal(page)
    await dump_html(page, f"{tag}_documents.html")
    return out


# --------------------------------------------------------------------------------------
# Download — two-step (Go Get It -> modal -> filename button -> expect_download)
# The document list is a virtualized Vaadin Grid (~8 rows in the DOM at once), so to reach more
# than ~8 files we scroll the grid and de-dupe by filename. Waits are event-based; the only fixed
# delay is POLITE_DELAY_S between downloads.
# --------------------------------------------------------------------------------------
async def scroll_doc_list(page, dy=520) -> None:
    """Scroll the virtualized Vaadin Grid down to render the next rows, then wait for the
    Go-Get-It buttons to settle (event-based, not a blind sleep)."""
    try:
        grid = page.locator(".v-grid").first
        if await grid.count() == 0:
            grid = page.locator(".v-panel-content").first
        box = await grid.bounding_box()
        if not box:
            return
        before = await page.get_by_role("button", name="Go Get It", exact=False).count()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await page.mouse.wheel(0, dy)
        # wait until the rendered Go-Get-It set stabilises (virtualization re-render finished)
        last, stable = before, 0
        for _ in range(20):
            await page.wait_for_timeout(120)
            now = await page.get_by_role("button", name="Go Get It", exact=False).count()
            stable = stable + 1 if now == last else 0
            last = now
            if stable >= 3:
                break
    except Exception as e:
        log(f"WARN scroll_doc_list: {e}")


async def open_download_modal(page, gg_btn):
    """Click a Go-Get-It button and return (modal, file_button_locator, [filenames]) once the
    Download Files modal and its per-file button are actionable. Event-based, no fixed pad."""
    await gg_btn.click()
    modal = page.locator(".v-window")
    await modal.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
    file_btn = modal.get_by_role(
        "button", name=re.compile(r"\.(pdf|docx?|xlsx?|tiff?|jpe?g|zip|csv|txt)$", re.I))
    try:
        await file_btn.first.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
    except PWTimeout:
        file_btn = modal.locator("button", has_text=re.compile(r"\.\w{2,4}$"))
        await file_btn.first.wait_for(state="visible", timeout=4_000)
    names = [(await file_btn.nth(j).inner_text()).strip()
             for j in range(await file_btn.count())]
    return modal, file_btn, names


async def close_modal(page) -> None:
    """Close the Download Files modal (Close/OK, fallback Escape) and wait for the curtain gone."""
    try:
        close = page.get_by_role("button", name="Close", exact=False)
        if await close.count() > 0 and await close.first.is_visible():
            await close.first.click()
        else:
            await page.keyboard.press("Escape")
    except Exception:
        await page.keyboard.press("Escape")
    await wait_curtain_gone(page)


async def download_n(page, rec: NetRecorder, tag, target_type, how_many=2) -> dict:
    findings = {"path": None, "mechanism": "", "attempts": [], "files": [],
                "target_type": target_type, "requested": how_many}
    await dismiss_modal(page)                            # clear any leftover curtain
    btn = page.get_by_role("button", name=target_type, exact=False)
    if await btn.count() == 0:
        findings["path"] = f"BLOCKED: no '{target_type}' tab button"
        return findings
    await btn.first.click()
    # wait for the document list (Go Get It buttons) to render — event-based
    gg = page.get_by_role("button", name="Go Get It", exact=False)
    try:
        await gg.first.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
    except PWTimeout:
        findings["path"] = "BLOCKED: no Go Get It buttons rendered"
        return findings
    findings["go_get_it_visible"] = await gg.count()
    log(f"'{target_type}' tab: {findings['go_get_it_visible']} Go Get It buttons visible; "
        f"requesting {how_many} downloads")

    wall_start = time.monotonic()
    done_names = set()
    idx = 0
    no_progress_scrolls = 0
    while len(done_names) < how_many and no_progress_scrolls < 3:
        gg = page.get_by_role("button", name="Go Get It", exact=False)
        k = await gg.count()
        progressed = False
        for i in range(k):
            if len(done_names) >= how_many:
                break
            attempt = {"seq": idx}
            rec.events = []
            rec.active = True
            t = time.monotonic()
            try:
                modal, file_btn, names = await open_download_modal(page, gg.nth(i))
            except Exception as e:
                rec.active = False
                log(f"WARN modal open (row {i}): {str(e).splitlines()[0]}")
                continue
            primary = names[0] if names else ""
            if primary in done_names:                  # row already downloaded (scroll overlap)
                rec.active = False
                await close_modal(page)
                continue
            attempt["modal_files"] = names
            try:
                async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as di:
                    await file_btn.first.click()
                d = await di.value
                fname = d.suggested_filename or f"{tag}_{idx}.bin"
                dest = DL_DIR / f"{tag}_{idx}_{fname}"
                await d.save_as(str(dest))
                attempt.update(
                    path="a) two-step: Go Get It -> modal -> file button -> expect_download",
                    filename=fname, download_url=d.url, saved=str(dest),
                    file_kind=sniff_kind(dest), bytes=dest.stat().st_size)
                findings["path"] = attempt["path"]
                findings["mechanism"] = (
                    "Go Get It opens a .v-window modal with a per-file button; clicking it triggers "
                    "page.expect_download from /fmi/webd/APP/connector/<n>/<n>/dl/<file> "
                    "(content-disposition: attachment).")
                done_names.add(primary or fname)
                progressed = True
                log(f"DOWNLOAD {len(done_names)}/{how_many}: {fname} "
                    f"({attempt['file_kind']}, {attempt['bytes']}B) in {round(time.monotonic()-t,2)}s")
            except Exception as e:
                attempt["error"] = f"file-button download failed: {str(e).splitlines()[0]}"
                log(attempt["error"])
            attempt["timing_s"] = round(time.monotonic() - t, 2)   # gg click -> saved (no close)
            rec.active = False
            (ART / f"network_goget_{tag}_{idx}.json").write_text(json.dumps(rec.events, indent=2))
            await close_modal(page)
            await page.wait_for_timeout(int(POLITE_DELAY_S * 1000))  # deliberate politeness gap
            findings["attempts"].append(attempt)
            idx += 1
        if len(done_names) >= how_many:
            break
        # need more than the currently-rendered rows -> scroll the virtualized grid
        await scroll_doc_list(page)
        no_progress_scrolls = 0 if progressed else no_progress_scrolls + 1

    findings["downloaded"] = len(done_names)
    findings["wall_clock_s"] = round(time.monotonic() - wall_start, 2)
    ok = [a for a in findings["attempts"] if a.get("bytes")]
    if ok:
        findings["per_file_timing_s"] = [a["timing_s"] for a in ok]
        findings["avg_overhead_excl_largest_s"] = round(
            sum(sorted(a["timing_s"] for a in ok)[:-1]) / max(1, len(ok) - 1), 2) if len(ok) > 1 else None
    if findings["downloaded"] < how_many:
        log(f"NOTE only {findings['downloaded']}/{how_many} downloaded "
            f"(tab has fewer docs or list end reached)")
    findings["files"] = [str(f) for f in DL_DIR.glob(f"{tag}_*") if f.is_file()]
    return findings


def sniff_kind(path):
    try:
        head = path.read_bytes()[:8]
    except Exception:
        return "unreadable"
    if head[:4] == b"%PDF":
        return "PDF"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if head[:2] == b"PK":
        return "ZIP/Office"
    return f"other({head!r})"


# --------------------------------------------------------------------------------------
# One full pass
# --------------------------------------------------------------------------------------
async def run_flow(p, headless, matter, tag, do_downloads=True, how_many=2, target_tab=None):
    result = {"matter": matter, "headless": headless, "tag": tag, "errors": []}
    rec = NetRecorder()
    browser = await p.chromium.launch(headless=headless, slow_mo=0 if headless else SLOW_MO)
    context = await browser.new_context(accept_downloads=True, user_agent=USER_AGENT)
    context.set_default_timeout(20_000)
    await context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = await context.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT)
    page.on("download", lambda d: log(f"DOWNLOAD EVENT: {d.suggested_filename}"))
    page.on("popup", lambda pg: log(f"POPUP EVENT: {pg.url}"))
    page.on("request", rec.on_request)
    page.on("response", rec.on_response)

    try:
        log(f"=== goto {URL} (headless={headless}, matter={matter}) ===")
        t = time.monotonic()
        await page.goto(URL, wait_until="domcontentloaded")
        await wait_for_app_ready(page)
        note(f"{tag}.land_seconds", round(time.monotonic() - t, 2))
        await shot(page, f"{tag}_01_landing.png")
        await dump_html(page, f"{tag}_landing.html")

        note(f"{tag}.frames", describe_frames(page))
        await widget_inventory(page, f"{tag}_landing")

        search_info = await enter_matter_and_search(page, matter)
        note(f"{tag}.search_info", search_info)
        await shot(page, f"{tag}_03_results.png")
        await dump_html(page, f"{tag}_results.html")
        await a11y_snapshot(page, f"{tag}_a11y_results.json")

        mc = await scrape_metadata_and_counts(page)
        note(f"{tag}.metadata", mc["metadata"])
        note(f"{tag}.counts", mc["counts"])
        result["metadata"] = mc["metadata"]
        result["counts"] = mc["counts"]

        per_tab = await survey_tabs(page, tag)
        result["per_tab"] = per_tab

        if do_downloads:
            target = target_tab or next((t["type"] for t in per_tab if t["go_get_it"] > 0),
                                        "Other Documents")
            dl = await download_n(page, rec, tag, target, how_many=how_many)
            result["downloads"] = dl
            note(f"{tag}.download_path", dl.get("path"))
            note(f"{tag}.download_summary",
                 {"requested": dl.get("requested"), "downloaded": dl.get("downloaded"),
                  "wall_clock_s": dl.get("wall_clock_s"),
                  "per_file_timing_s": dl.get("per_file_timing_s")})
        else:
            result["downloads"] = {"skipped": "politeness — headless verification pass"}
            log("downloads skipped this pass (politeness)")
        await shot(page, f"{tag}_05_post.png")

    except Exception as e:
        log("ERROR in run_flow: " + str(e))
        log(traceback.format_exc())
        result["errors"].append(str(e))
        await shot(page, f"{tag}_ERROR.png")
        await dump_html(page, f"{tag}_ERROR.html")
    finally:
        try:
            await context.tracing.stop(path=str(ART / f"trace_{tag}.zip"))
        except Exception as e:
            log(f"WARN tracing.stop: {e}")
        (ART / f"network_{tag}.json").write_text(json.dumps(rec.events, indent=2))
        await context.close()
        await browser.close()
    return result


def parse_args():
    ap = argparse.ArgumentParser(description="UARB scrape spike")
    ap.add_argument("--max-downloads", type=int, default=2,
                    help="number of documents to download in the primary pass (default 2)")
    ap.add_argument("--headless", action="store_true",
                    help="run the primary pass headless (production-like; for speed measurement)")
    ap.add_argument("--target-tab", default=None,
                    help="document-type tab to download from (default: first non-empty)")
    ap.add_argument("--skip-headless-check", action="store_true",
                    help="skip the extra headless verification pass")
    ap.add_argument("--matter", default=MATTER, help=f"matter number (default {MATTER})")
    return ap.parse_args()


async def main(args):
    log(f"=== SPIKE START {now_iso()} (max_downloads={args.max_downloads} "
        f"headless={args.headless} target_tab={args.target_tab}) ===")
    results = {}
    async with async_playwright() as p:
        results["primary"] = await run_flow(
            p, headless=args.headless, matter=args.matter, tag="m12205", do_downloads=True,
            how_many=args.max_downloads, target_tab=args.target_tab)
        # Extra headless verification pass (no downloads) — only when the primary was headed.
        if not args.skip_headless_check and not args.headless:
            try:
                results["headless"] = await run_flow(
                    p, headless=True, matter=args.matter, tag="m12205_headless",
                    do_downloads=False)
            except Exception as e:
                log(f"headless run failed: {e}")
                results["headless"] = {"errors": [str(e)]}
    (ART / "discovery.json").write_text(
        json.dumps({"notes": NOTES, "results": results}, indent=2, default=str))
    dl = (results.get("primary") or {}).get("downloads") or {}
    log(f"=== SPIKE DONE === downloaded {dl.get('downloaded')}/{dl.get('requested')} "
        f"in wall_clock={dl.get('wall_clock_s')}s; per-file={dl.get('per_file_timing_s')}")
    log("wrote discovery.json; artifacts in ./spike-artifacts/")


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
