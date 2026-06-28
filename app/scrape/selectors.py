"""Content-anchored locators and timeouts for the UARB FileMaker WebDirect portal (spec §7.6).

Every value here is lifted verbatim from the validated spike (``reference/spike.py`` +
``reference/FINDINGS.md``) — confirmed working against the live site for M12205, headed and
headless. Anchor on human-authored text (placeholders, button captions), never on FileMaker's
generated ids (e.g. ``b0p0o254i0i0r1``), which churn.
"""

from __future__ import annotations

import re

URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"

#: The "eg M01234" prompt that uniquely identifies the matter input box.
MATTER_PLACEHOLDER = "M01234"

#: The five document-type tabs, in the site's tab order. Counts ride in the button labels
#: ("Exhibits - 13"), and the empty ones (count 0) raise the "No Matching Records" modal.
DOC_TYPES: list[str] = [
    "Exhibits",
    "Key Documents",
    "Other Documents",
    "Transcripts",
    "Recordings",
]

#: A realistic desktop Chrome UA (the spike used this exact string).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ── Timeouts (ms) — from spec §7.6 / the spike ───────────────────────────────
DOWNLOAD_TIMEOUT_MS = 90_000   # one Exhibit was 48 MB — don't time out large transfers
MODAL_TIMEOUT_MS = 12_000      # the "Download Files" modal + its file button
CURTAIN_TIMEOUT_MS = 8_000     # the modality curtain clearing after a modal closes
APP_READY_TIMEOUT_MS = 30_000  # FileMaker widgets painting after goto
RESULTS_TIMEOUT_MS = 30_000    # the "<Type> - <N>" results signal after Search
DEFAULT_TIMEOUT_MS = 20_000    # Playwright default action timeout (spike used 20s)
NAV_TIMEOUT_MS = 60_000        # navigation timeout

# ── Field-edit timing (FileMaker server round-trips) ─────────────────────────
EDIT_MODE_WAIT_MS = 700        # after clicking the matter field, before typing
TYPE_DELAY_MS = 60             # per-keystroke delay when typing the matter number
POST_TYPE_WAIT_MS = 400        # settle after typing

# ── Regex helpers ────────────────────────────────────────────────────────────
#: Match a document-type count in a button/body label, e.g. "Other Documents - 42".
def count_re(doc_type: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(doc_type)}\s*-\s*(\d+)")

#: A file-button label inside the "Download Files" modal (by known extension).
FILE_BUTTON_RE = re.compile(r"\.(pdf|docx?|xlsx?|tiff?|jpe?g|zip|csv|txt)$", re.I)

#: Broad fallback (from the spike): ANY 2–4 char extension. Catches corpus file types not in the
#: known list above (e.g. audio/video on Recordings/Transcripts) so those modals still resolve.
FILE_BUTTON_FALLBACK_RE = re.compile(r"\.\w{2,4}$")

#: Dates on the results screen are MM/DD/YYYY (NOT month-name format).
DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")

#: The dollar amount rides inside the title line "{org} - {project} - $amount".
AMOUNT_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")

#: Matter number as it appears in body text.
MATTER_IN_BODY_RE = re.compile(r"\bM\d{4,6}\b")
