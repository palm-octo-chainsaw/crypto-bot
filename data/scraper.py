"""Scrape RSPS signal allocations from The Real World."""

import logging
import os
import re
import tempfile
from playwright.async_api import async_playwright, TimeoutError as PwTimeout
import pyotp

from constants import TRW_EMAIL, TRW_PASSWORD, TRW_TOTP_SECRET, TRW_SIGNAL_URL

logger = logging.getLogger(__name__)

ALLOCATION_LINE = re.compile(r"([\d.]+)%\s+(\w+)", re.IGNORECASE)
CASH_ALIASES = {"cash", "usd", "stables", "stable"}
KNOWN_TOKENS = {
    "BTC", "ETH", "SOL", "SUI", "XRP", "DOGE", "LINK", "BNB",
    "USDC", "USDT", "PAXG", "HYPE", "ADA", "AVAX", "DOT", "MATIC",
    "SHIB", "UNI", "AAVE", "ARB", "OP", "NEAR", "FTM", "ATOM",
    "CASH", "USD", "STABLES", "STABLE",
}

SESSION_DIR = os.path.join(os.path.dirname(__file__), "..", ".trw_session")
DEBUG_DIR = os.getenv("TRW_DEBUG_DIR", tempfile.gettempdir())
DEBUG_SCREENSHOT = os.path.join(DEBUG_DIR, "trw_debug.png")


def parse_signal(text: str) -> dict[str, float]:
    lower = text.lower()
    start = lower.find("rsps signal")
    if start == -1:
        return {}

    section = text[start:]
    for delimiter in ["Executive Summary", "Associated Data", "———", "─────"]:
        end = section.find(delimiter)
        if end > 0:
            section = section[:end]
            break

    allocations = {}
    for match in ALLOCATION_LINE.finditer(section):
        pct = float(match.group(1))
        symbol = match.group(2).upper()
        if symbol not in KNOWN_TOKENS:
            continue
        if symbol.lower() in CASH_ALIASES:
            symbol = "USDC"
        if symbol in allocations:
            logger.info("Duplicate allocation for %s — aggregating %.2f + %.2f", symbol, allocations[symbol], pct)
            allocations[symbol] += pct
        else:
            allocations[symbol] = pct

    total_allocation = sum(allocations.values())
    if total_allocation == 0:
        logger.warning("Parsed allocations sum to 0%% — returning empty")
        return {}

    if not (95.0 <= total_allocation <= 105.0):
        logger.warning(
            "Parsed allocations sum to %.2f%% (outside [95%%, 105%%]); normalizing to 100%%",
            total_allocation,
        )
        factor = 100.0 / total_allocation
        for symbol in allocations:
            allocations[symbol] *= factor

    return allocations


async def _login(page) -> None:
    """Perform full login flow: email/password + TOTP."""
    logger.info("[TRW] Clicking login button...")
    login_btn = page.get_by_text("LOGIN TO YOUR ACCOUNT", exact=False)
    if await login_btn.count() == 0:
        login_btn = page.get_by_text("Login", exact=False)
    await login_btn.first.wait_for(timeout=10000)
    await login_btn.first.click()
    await page.wait_for_timeout(3000)

    logger.info("[TRW] Filling login form...")
    email_input = page.locator('input[type="email"], input[name="email"], input[placeholder*="mail"], input[placeholder*="Email"]')
    await email_input.first.wait_for(timeout=15000)
    await email_input.first.fill(TRW_EMAIL)

    password_input = page.locator('input[type="password"]')
    await password_input.first.wait_for(timeout=5000)
    await password_input.first.fill(TRW_PASSWORD)

    submit_btn = page.get_by_text("Log In", exact=False)
    await submit_btn.first.click()
    await page.wait_for_timeout(5000)

    logger.info("[TRW] Entering TOTP code...")
    totp = pyotp.TOTP(TRW_TOTP_SECRET)
    code = totp.now()

    totp_input = None
    for selector in [
        'input[inputmode="numeric"]',
        'input[type="tel"]',
        'input[type="number"]',
        'input[autocomplete="one-time-code"]',
        'input[placeholder*="code" i]',
        'input:not([type="email"]):not([type="password"])',
    ]:
        candidate = page.locator(selector)
        if await candidate.count() > 0 and await candidate.first.is_visible():
            totp_input = candidate.first
            break

    if totp_input is None:
        await page.screenshot(path=DEBUG_SCREENSHOT, full_page=False)
        raise RuntimeError(f"Could not find TOTP input field. Check {DEBUG_SCREENSHOT}")

    await totp_input.wait_for(timeout=10000)
    await totp_input.click()
    await page.keyboard.type(code, delay=100)
    await page.wait_for_timeout(1000)

    confirm_btn = page.get_by_text("Confirm", exact=False)
    await confirm_btn.first.click(force=True)
    await page.wait_for_timeout(5000)


async def _handle_device_limit(page) -> None:
    """Dismiss device-limit modal by logging out the oldest non-current sessions."""
    logger.info("[TRW] Device limit modal detected — removing old sessions...")

    for _ in range(5):
        modal = page.get_by_text("Device Limit Reached", exact=False)
        if await modal.count() == 0:
            break

        logout_btns = page.locator('button:has-text("Logout")')
        count = await logout_btns.count()
        if count == 0:
            break

        logger.info("[TRW] Found %d logout buttons, clicking last...", count)
        await logout_btns.last.click(force=True)
        await page.wait_for_timeout(3000)

    # Try closing the modal if still present
    close_btn = page.locator('button:has(svg.lucide-x)')
    if await close_btn.count() > 0:
        try:
            await close_btn.first.click(force=True, timeout=3000)
        except Exception:
            pass
    await page.wait_for_timeout(2000)

    logger.info("[TRW] Device limit resolved")


async def _extract_signal(page) -> dict[str, float]:
    """Extract RSPS signal from the loaded channel page."""
    messages = page.locator('[class*="message"], [class*="chat"], [class*="post"]')
    count = await messages.count()
    logger.info("[TRW] Found %d message elements", count)

    signal_text = None
    for idx in range(count - 1, max(count - 20, -1), -1):
        text = await messages.nth(idx).inner_text()
        if "rsps signal" in text.lower():
            signal_text = text
            logger.info("[TRW] Found signal message at index %d", idx)
            break

    if not signal_text:
        body_text = await page.inner_text("body")
        if "rsps signal" in body_text.lower():
            start = body_text.lower().index("rsps signal")
            signal_text = body_text[start:]
            logger.info("[TRW] Found signal in page body text")

    if not signal_text:
        await page.screenshot(path=DEBUG_SCREENSHOT, full_page=False)
        raise RuntimeError(f"Could not find RSPS signal on page. Check {DEBUG_SCREENSHOT}")

    allocations = parse_signal(signal_text)
    if not allocations:
        raise RuntimeError(f"Found signal text but could not parse allocations:\n{signal_text[:300]}")

    logger.info("[TRW] Parsed allocations: %s", allocations)
    return allocations


async def _extract_timestamps(page) -> list[str]:
    """Extract message timestamps from the loaded channel page."""
    body_text = await page.inner_text("body")
    # Match patterns like "Today at 3:10 AM", "Yesterday at 10:00 PM", "04/09/2026 3:10 AM"
    patterns = re.findall(
        r"(?:Today|Yesterday|\d{1,2}/\d{1,2}/\d{2,4})\s+(?:at\s+)?\d{1,2}:\d{2}\s*(?:AM|PM)",
        body_text, re.IGNORECASE,
    )
    if not patterns:
        # Try just time-of-day patterns
        patterns = re.findall(r"\d{1,2}:\d{2}\s*(?:AM|PM)", body_text, re.IGNORECASE)
    return patterns


async def _open_channel(p, *, save_session: bool = True):
    """Open signal channel, handling login and device limits. Returns (browser, context, page)."""
    session_path = os.path.abspath(SESSION_DIR)
    state_file = os.path.join(session_path, "state.json")

    # Try reusing saved session first
    if os.path.isfile(state_file):
        logger.info("[TRW] Reusing saved session...")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=state_file,
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()
        await page.goto(TRW_SIGNAL_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        # Check if we're still logged in
        login_btn = page.get_by_text("LOGIN TO YOUR ACCOUNT", exact=False)
        if await login_btn.count() > 0:
            logger.info("[TRW] Session expired, logging in again...")
            await browser.close()
        else:
            # Handle device limit if it appears
            device_limit = page.get_by_text("Device Limit Reached", exact=False)
            if await device_limit.count() > 0:
                await _handle_device_limit(page)
                await page.goto(TRW_SIGNAL_URL, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(5000)
            return browser, context, page

    # Fresh login
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    page = await context.new_page()
    await page.goto(TRW_SIGNAL_URL, wait_until="networkidle", timeout=30000)
    await _login(page)

    # Handle device limit if it appears after login
    device_limit = page.get_by_text("Device Limit Reached", exact=False)
    if await device_limit.count() > 0:
        await _handle_device_limit(page)
        await page.goto(TRW_SIGNAL_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(5000)

    # Save session for reuse
    if save_session:
        os.makedirs(session_path, exist_ok=True)
        await context.storage_state(path=state_file)
        logger.info("[TRW] Session saved to %s", session_path)

    return browser, context, page


async def fetch_signal() -> dict[str, float]:
    if not all([TRW_EMAIL, TRW_PASSWORD, TRW_TOTP_SECRET]):
        raise ValueError("TRW_EMAIL, TRW_PASSWORD, and TRW_TOTP_SECRET must be set in .env")

    browser = None
    page = None
    async with async_playwright() as p:
        try:
            browser, context, page = await _open_channel(p)
            await page.wait_for_timeout(5000)
            return await _extract_signal(page)
        except PwTimeout as err:
            if page:
                try:
                    await page.screenshot(path=DEBUG_SCREENSHOT, full_page=False)
                except Exception:
                    pass
            logger.error("[TRW] Timeout — screenshot saved to %s", DEBUG_SCREENSHOT)
            raise RuntimeError(f"TRW page timed out: {err}") from err
        finally:
            if browser:
                await browser.close()


async def fetch_timestamps() -> dict:
    """Fetch message timestamps from the TRW signal channel."""
    if not all([TRW_EMAIL, TRW_PASSWORD, TRW_TOTP_SECRET]):
        raise ValueError("TRW_EMAIL, TRW_PASSWORD, and TRW_TOTP_SECRET must be set in .env")

    browser = None
    async with async_playwright() as p:
        try:
            browser, context, page = await _open_channel(p)
            await page.wait_for_timeout(5000)
            timestamps = await _extract_timestamps(page)

            # Also try getting timestamps from DOM attributes
            dom_times = await page.evaluate(r'''() => {
                const results = [];
                const els = document.querySelectorAll("*");
                for (const el of els) {
                    for (const attr of el.attributes) {
                        if (/time|date|stamp/i.test(attr.name) && /\d/.test(attr.value)) {
                            results.push({attr: attr.name, value: attr.value, tag: el.tagName, text: el.textContent.trim().substring(0, 80)});
                        }
                    }
                }
                return results;
            }''')

            # Scroll up to load older messages and capture more timestamps
            for _ in range(5):
                await page.keyboard.press("PageUp")
                await page.wait_for_timeout(2000)

            body_after_scroll = await page.inner_text("body")
            more_times = re.findall(
                r"(?:Today|Yesterday|\d{1,2}/\d{1,2}/\d{2,4})\s+(?:at\s+)?\d{1,2}:\d{2}\s*(?:AM|PM)",
                body_after_scroll, re.IGNORECASE,
            )

            return {
                "visible_timestamps": timestamps,
                "after_scroll": more_times,
                "dom_attributes": dom_times[:30],
            }
        finally:
            if browser:
                await browser.close()
