"""Browser factory (spec §7.6).

The ONLY place that names the browser engine, so a later camoufox/stealth swap is one function.
Owns the headless flag, ``slow_mo``, a realistic desktop UA, ``accept_downloads``, and the default
timeout. Returns ``(browser, context)``; the caller opens pages and is responsible for closing.
"""

from __future__ import annotations

from playwright.async_api import Browser, BrowserContext, Playwright

from app.config import Settings
from app.scrape import selectors


async def launch_browser(
    playwright: Playwright, settings: Settings
) -> tuple[Browser, BrowserContext]:
    """Launch Chromium and a download-enabled context per the environment settings.

    Local: headed with ``slow_mo`` (for watching). Prod: headless. Tracing is started by the
    caller (the scraper) so it can attach to the failing step.
    """
    browser = await playwright.chromium.launch(
        headless=settings.headless,
        slow_mo=settings.slow_mo_ms,
    )
    context = await browser.new_context(
        accept_downloads=True,
        user_agent=selectors.USER_AGENT,
    )
    context.set_default_timeout(selectors.DEFAULT_TIMEOUT_MS)
    return browser, context
