# Scrape Spike — FINDINGS (Nova Scotia UARB Public Documents Database)

**Target:** `https://uarb.novascotia.ca/fmi/webd/UARB15` — a **FileMaker WebDirect** app built on
**Vaadin 8** (GWT widgetset `com.filemaker.jwpc.iwp.widgetset.UIWidgetSet`, `vaadinBootstrap.js
?v=8.27.7.fmi`, Atmosphere push).
**Test matter:** M12205. **Run date:** 2026-06-28. **Playwright:** Python async, Chromium.
All selectors below are **confirmed working against the live site**, captured in
`./spike-artifacts/` (screenshots, HTML dumps, traces, network logs, 2 downloaded PDFs).

---

## 1. Verdict

**The full flow works end to end, headed AND headless: search → results → metadata → all five
counts → two real PDF downloads.** Both passes completed with zero errors
(`discovery.json: results.primary.errors == []` and `results.headless.errors == []`). Two PDFs
were downloaded and verified (`%PDF`, PDF 1.7). **No blocker.**

The one thing a naive scraper *will* get wrong is the download: it is **not** a link or a single
click — it is a **two-step modal** flow, and empty document tabs raise a **blocking modal** that
must be dismissed. Both are handled and documented below.

---

## 2. Answers to the unknowns (1–10)

### 1. Frame
**Not in a content iframe.** The app paints into the **top document**. `page.frames` returns two
frames: the main document, and a second frame
`name="com.filemaker.jwpc.iwp.widgetset.UIWidgetSet"` whose URL is **`about:blank`** — this is the
GWT history/communication frame, **not** content. **All locators target `page` directly; no
`frame_locator` needed.** (Container/PDF downloads also do *not* open a viewer frame — see §3.)

### 2. Matter input + Search
The page is **not** standard HTML — there are **zero `<input>` elements**. FileMaker renders text
fields as `div.fm-textarea` containing an editable `div.text[tabindex=0]` plus a `.placeholder`
span. So `fill()`/`get_by_label`/`input.v-textfield` all **fail**.

- **Matter box (stable, content-anchored):**
  ```python
  field = page.locator(".fm-textarea").filter(
      has=page.locator(".placeholder", has_text="M01234"))   # the 'eg M01234' prompt
  await field.locator(".text").click()
  await page.wait_for_timeout(700)        # FileMaker enters field-edit mode (server round-trip)
  await page.keyboard.type("M12205", delay=60)               # type, do NOT fill()
  ```
  Resolves to exactly **1** element. Anchoring on the human-authored placeholder text "eg M01234"
  is far more stable than the generated id (`b0p0o254i0i0r1`) or FileMaker object class
  (`fm_object_254`), both of which can change.

- **Search button:** buttons are real `<button>` with an accessible name, so
  `get_by_role("button", name="Search")` matches — **but there are 5 matching "Search" buttons**
  (top-nav, the matter box, the criteria form, etc.). Pick the matter-box one **by geometry** (the
  Search button on the same row as the matter field). In this run it was index 4, `dy = 2px`:
  ```python
  fbox = await field.bounding_box()
  search = page.get_by_role("button", name="Search", exact=False)
  # choose the candidate with min |vertical-center - field-center| and x to the right of the field
  ```
  Stability: medium-high. The geometry heuristic is robust to id churn; if the layout is
  redesigned it would need revisiting.

### 3. Search response — navigation vs AJAX, signal, timing
- **In-place AJAX** (Vaadin UIDL), **URL does not change** (`search_navigation:
  "in-place-AJAX(same URL)"`).
- **Reliable signal:** the document-type labels appear as `"<Type> - <N>"` in the page text.
  We poll for `"Exhibits -"` / `"Other Documents -"` etc.:
  ```python
  await page.wait_for_function-equivalent:  body includes any of "Exhibits -", "Key Documents -", ...
  ```
- **Timing: ~0.55s** (very fast). `networkidle` is **not** usable as a signal (the Atmosphere push
  connection + ~30s heartbeat keep the network busy) — that's why we poll on content.

### 4. Tabs + counts
The five document "tabs" are **`<button>`s in a row, with the count embedded in the label**:
`"Exhibits - 13"`, `"Key Documents - 5"`, `"Other Documents - 42"`, `"Transcripts - 0"`,
`"Recordings - 0"` (plus "Hearings", "Related Matters"). They are **not** `role=tab` / Vaadin
tabsheet elements.

- **All five counts are readable on the results screen without opening any tab**, via one regex
  pass over `document.body.innerText`: `rf"{type}\s*-\s*(\d+)"`.
- To open a type: `page.get_by_role("button", name="Other Documents", exact=False).click()`.
- **Gotcha:** opening a **zero-count** tab (Transcripts, Recordings) pops a blocking modal — see §5/Gotchas.

### 5. Metadata location
All summary metadata is plain text on the results screen (also present in `.v-label`/`.v-caption`
nodes, dumped to `results_labels.json`). Confirmed extraction:

| Field | Where / how read |
|---|---|
| Matter No | `\bM\d{5}\b` → `M12205` |
| Title + Amount | the one body line containing `" - $"` → `"Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000"`; amount via `\$[\d,]+` |
| Status | standalone line → `"Awaiting Compliance"` |
| Type | `"Capital Expenditure Approvals"` |
| Category | `"Water"` |
| Date Received (initial filing) | first `\b\d{2}/\d{2}/\d{4}\b` → `04/07/2025` |
| Date Final (final filing) | second date → `10/23/2025` |

**Note the date format is `MM/DD/YYYY`**, not "Month D, YYYY" (a naive `[A-Z][a-z]+ \d, \d{4}`
regex finds **nothing**). Stability: high for text presence; the amount currently rides inside the
title line, so parse it out rather than assuming a dedicated field.

### 6. "Go Get It" download — **the mechanism (most important finding)**
**Path (a), but TWO STEP — not a single click:**

1. Each document row has a **`Go Get It`** `<button>` (`get_by_role("button", name="Go Get It")`).
   The list is **virtualized**: ~**8** Go-Get-It buttons are in the DOM at once even when the tab
   has 42 docs (you'd scroll to load more).
2. Clicking `Go Get It` opens an **in-page Vaadin modal** `div.v-window` titled **"Download
   Files"** ("Your files are ready for download. Please click the button to download each file:")
   containing **one `<button>` per file, labelled `<docno>.<ext>`** (e.g. `H-1.pdf`).
3. Clicking that **filename button fires a real browser download**, caught cleanly by
   **`page.expect_download()`**:
   ```python
   await go_get_it.nth(i).click()
   modal = page.locator(".v-window"); await modal.wait_for(state="visible")
   file_btn = modal.get_by_role("button", name=re.compile(r"\.(pdf|docx?|xlsx?|tiff?|jpe?g|zip)$", re.I))
   async with page.expect_download() as di:
       await file_btn.first.click()
   d = await di.value; await d.save_as(dest)
   ```
4. **Close the modal** before the next download (`Close`/`OK` button, fallback `Escape`).

**The file URL** (from the network log) is a FileMaker connector:
`https://uarb.novascotia.ca/fmi/webd/APP/connector/0/<n>/dl/<filename>` →
`200 application/octet-stream;charset=UTF-8`, `content-disposition: attachment;
filename*=UTF-8''H-1.pdf`. The `<n>` segment (2226, 2228, …) is **per-file/per-session** and
generated fresh each Go-Get-It click.

→ This is **path (a)**: `page.expect_download()` works directly. **You do NOT need
`context.request.get()` / in-session fetch**, and there is **no popup and no in-page viewer**
(paths b/c are false here). The connector URL is session-bound, but because the click triggers a
native download you never have to fetch it yourself.

### 7. Sequential vs parallel downloads — and how it scales (measured to 10)
**Sequential is the only safe option**: each Go-Get-It opens a *singleton* `.v-window` modal with
a modality curtain that blocks all other clicks, so concurrent downloads in one session are
impossible. (Parallel *sessions* would work but multiply load on a gov server — don't.)

`spike.py` takes **`--max-downloads N`** (default 2). Because the list is a virtualized Vaadin
Grid (~8 rows in the DOM at once), reaching N>~8 requires **scrolling the grid and de-duping by
filename** — both implemented (`scroll_doc_list`, `done_names` set). All waits are **event-based**
(wait for the modal/curtain condition, not a fixed sleep) — faster *and* more robust under load —
with a single deliberate **`POLITE_DELAY_S = 0.6s`** courtesy gap between downloads.

**Measured, headless (`--headless`, production-like), 10 docs from "Other Documents":**

| Metric | Value |
|---|---|
| Total download phase (10 files) | **14.05s** |
| Per-file, small docs (≤1 MB) | **~0.3s** (mechanical floor) |
| Per-file, the two 4 MB docs | ~1.0–1.5s (transfer-bound) |
| Politeness delay component | 10 × 0.6s = **6.0s** (the largest single chunk; tunable) |
| Modal close / curtain / scroll | ~2.7s total |
| Result | **10/10 valid PDFs, 0 duplicates** |

**Scaling model:** `T(N) ≈ one-time setup + N × (~0.3s mechanical + 0.6s politeness) + Σ transfer`.
So **10 files ≈ ~14s** of download phase plus ~8–12s one-time setup (a production scraper that
skips the 5-tab survey would shave most of the setup). There is **no hidden super-linear cost** —
overhead is flat per file. The two tunable levers are `POLITE_DELAY_S` (currently the biggest
controllable cost) and headless vs headed (`slow_mo`); transfer time is network-bound and
irreducible.

For reference, the *headed* run (`slow_mo=250`, for human watching) is slower per file —
e.g. H-2 (138 KB) took 1.19s headed vs 0.76s after the event-wait fix, and ~0.3s headless. Use
headless for the real deploy.

### 8. Filenames + types
`download.suggested_filename` is the real document name (`H-1.pdf`, `H-2.pdf`) — matches the modal
button label and the connector URL. Both downloads are **genuine PDFs** (`%PDF`, PDF 1.7,
verified with `file`). The document list shows a `.pdf` extension column and filter controls
(`PDF Only`, `Excel/Word/Other`, `All Types`), so **non-PDF types (Excel/Word/other) do exist in
the corpus generally** — none were among the Exhibit docs downloaded here, but the download code
accepts any extension (`\.(pdf|docx?|xlsx?|tiff?|jpe?g|zip)$`) and `sniff_kind()` classifies by
magic bytes (PDF/TIFF/JPEG/ZIP-Office).

### 9. Timing / flakiness / explicit waits
- **Explicit waits needed:** (a) `wait_for_app_ready` after `goto` — poll for `.fm-textarea`/
  `.fm-widget` present **and** `.v-loading-indicator` hidden (~3–5s headed, ~5–6s headless);
  (b) **700ms after clicking the matter field** before typing (FileMaker enters edit mode via a
  server round-trip — typing too early drops characters); (c) poll for the results signal after
  Search (~0.5s); (d) `wait_for(state="visible")` on the `.v-window` modal before clicking the
  file button.
- **Flaky spot:** the **"No Matching Records" modal** on empty tabs (§Gotchas). If not dismissed,
  its modality curtain makes the *next* click hang the full 20s timeout. The script now dismisses
  any open modal before each tab/download action.
- **Full cycle wall-clock:** landing→search→all 5 tabs→2 downloads ≈ **35s headed** (of which
  ~10s is the 48 MB file). The non-download path is ~16s headed.

### 10. Headless check (deploy target runs headless)
**Headless works.** A second pass with `headless=True` reproduced the entire flow — landing
(~6s), matter entry, search (0.55s), identical metadata, identical counts (13/5/42/0/0), all five
tabs incl. the empty-tab modals — with **zero errors**. Downloads were intentionally **skipped on
the headless pass for politeness** (to avoid re-pulling the 48 MB file), but every step up to the
Go-Get-It buttons behaves the same as headed. **No headless-specific breakage observed.**

---

## 3. Download mechanism (summary for the main build)

> **Path (a), two-step, session connector URL.**
> `Go Get It` (row button) → `.v-window` "Download Files" modal → click `<docno>.<ext>` button →
> `page.expect_download()` → save. File served from
> `/fmi/webd/APP/connector/0/<n>/dl/<filename>` as `application/octet-stream` +
> `content-disposition: attachment`. No popup, no viewer, no manual fetch. Close the modal between
> downloads; do them sequentially.

The main scraper does **not** need in-session `request.get()` plumbing — plain click-and-catch is
sufficient. It **does** need: virtualized-list scrolling to reach all N docs (only ~8
Go-Get-It buttons render at once), modal handling, and the empty-tab modal guard.

---

## 4. Metadata + counts vs the oracle

| Field | Oracle (spec) | Live (scraped) | Match |
|---|---|---|---|
| Title / subject | Halifax Regional Water Commission — Windsor Street Exchange Redevelopment Project | Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project | ✅ |
| Amount | **$69,270,000** | **$69,275,000** | ❌ **differs by $5,000** |
| Category | Capital Expenditure, Water | Type "Capital Expenditure Approvals" + Category "Water" | ✅ (same info, two fields) |
| Initial filing | April 7, 2025 | 04/07/2025 | ✅ |
| Final filing | October 23, 2025 | 10/23/2025 | ✅ |
| Exhibits | 13 | 13 | ✅ |
| Key Documents | 5 | 5 | ✅ |
| **Other Documents** | **21** | **42** | ❌ **differs (now 42)** |
| Transcripts | 0 | 0 | ✅ |
| Recordings | 0 | 0 | ✅ |

**Conclusion: this is unambiguously the right matter** (title + both dates + 3 of 5 counts match
exactly). The **two mismatches are real live-vs-spec drift**, not scraper error:
- **Other Documents 21 → 42:** the matter accreted more filings since the oracle was written
  (the document list shows dates running to **06/2026**, i.e. after the Oct-2025 "final filing").
- **Amount $69,270,000 → $69,275,000:** a $5k revision to the project value.

Per the spec's instruction to *confirm, not hardcode*: the oracle is **slightly stale**; the live
page is the source of truth. A scraper should read these live and not assert the spec's literals.

---

## 5. Gotchas / surprises (what trips a naive scraper)

1. **No `<input>` elements at all.** Fields are `div.fm-textarea` with an inner `div.text`; you
   **click then `keyboard.type`** — `fill()` does nothing. Anchor the matter box on its
   `.placeholder` text ("eg M01234"), not on generated ids.
2. **Buttons are plain `<button>`** (so role/name locators work) but **labels are ambiguous** —
   5 different "Search" buttons. Disambiguate by geometry.
3. **Counts live in the button labels** (`"Exhibits - 13"`), not in a tab/badge widget.
4. **`MM/DD/YYYY` dates** — not month-name format.
5. **Download is a two-step modal**, not a link/single click. (Highest-risk item; fully resolved.)
6. **Empty tabs raise a blocking modal** — clicking a 0-count tab (Transcripts/Recordings) pops a
   `.v-window` "No Matching Records" dialog whose **modality curtain blocks every subsequent
   click** until dismissed (`OK`/`Close`/`Escape`). Without this, the next action hangs 20s. This
   was the actual failure that broke the first full run; it is now handled by `dismiss_modal()`.
7. **`networkidle` is unusable** (Atmosphere push + heartbeat never idle) → poll on content.
8. **Virtualized document list** — only ~8 Go-Get-It buttons in the DOM regardless of the 42-doc
   count; reaching them all requires scrolling.
9. **GWT `about:blank` frame** in `page.frames` is a red herring — not content.
10. **Large files** — one Exhibit was **48 MB**. Budget for slow transfers / set generous download
    timeouts; don't assume documents are small.
11. **307 redirect + `JSESSIONID` cookie** scoped to `/fmi` is set on first load; Playwright's
    context handles it automatically.

---

## 6. Does the main plan still hold?

**Yes — with these load-bearing adjustments baked in:**

- **Driver:** Playwright (real browser) is mandatory; the app is 100% client-rendered. Works
  **headless** (deploy-ready). Keep `domcontentloaded` + content-polling, **never `networkidle`**.
- **Locators:** FileMaker `fm-widget`/`fm-textarea` DOM, **not** stock Vaadin or HTML inputs.
  Content-anchored locators (placeholder text, button captions) over generated ids.
- **Download:** click-and-catch (`expect_download`) is enough — **no in-session fetch needed**
  (path a, not c/d). But the scraper must implement the **two-step modal**, **modal dismissal**
  (incl. the empty-tab "No Matching Records" guard), and **list virtualization scrolling** to
  reach every document. Sequential downloads only.
- **Data:** read metadata/counts live and treat the spec oracle as stale (amount and the
  Other-Documents count have already drifted). Expect non-PDF file types in the broader corpus.
- **Politeness:** single matter, `slow_mo`, inter-action delays, sequential downloads — all fine
  against this gov site (no `robots.txt`; one `JSESSIONID` session).

No architectural change required: the original "drive the WebDirect UI with Playwright and catch
downloads" plan stands. The risk that would have changed the architecture (download path c/d
needing authenticated in-session fetching) **did not materialize** — it's a clean path (a).

---

## 7. Artifacts saved in `./spike-artifacts/`

**Code:** `../spike.py` (final, validated script) · `../probe.py`, `../probe2.py`, `../probe3.py`
(throwaway discovery probes that nailed the field-typing, tab, and download steps).

**Evidence (headed run `m12205_*`, headless run `m12205_headless_*`):**
- `trace_m12205.zip` (4.0M), `trace_m12205_headless.zip` (3.0M) — **Playwright traces**, open with
  `playwright show-trace spike-artifacts/trace_m12205.zip`.
- Screenshots: `*_01_landing.png`, `*_03_results.png`, `*_05_post.png`, and `*_tab_<Type>.png` for
  all five tabs (the Transcripts/Recordings shots capture the "No Matching Records" modal).
- HTML dumps: `*_landing.html`, `*_results.html`, `*_documents.html`.
- `m12205_a11y_results.json` — accessibility tree (aria snapshot) of the results screen.
- `results_text.txt`, `results_labels.json` — raw results-screen text + all label/caption strings.
- `inventory_*_landing.json` — widget inventories (buttons/fields/captions).
- Network logs: `network_m12205.json` (full session), `network_goget_m12205_0.json` /
  `_1.json` (the two Go-Get-It clicks, incl. the connector response).
- `discovery.json` — structured machine-readable notes + per-pass results.
- `downloads/m12205_0_H-1.pdf` (48 MB), `downloads/m12205_1_H-2.pdf` (138 KB) — **the actual
  downloaded PDFs** (verified `%PDF`, PDF 1.7).
- `run.log`, `stdout.log` — full timestamped run logs.
