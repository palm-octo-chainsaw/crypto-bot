"""Scrape RSPS signal allocations from The Real World."""

import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from playwright.async_api import async_playwright, TimeoutError as PwTimeout
import pyotp

from constants import TRW_EMAIL, TRW_PASSWORD, TRW_TOTP_SECRET, TRW_SIGNAL_URL

logger = logging.getLogger(__name__)

SIGNAL_MARKER = "rsps signal"
ALLOCATION_LINE = re.compile(r"([\d.]+)%\s+(\w+)", re.IGNORECASE)
CASH_ALIASES = {"cash", "usd", "stables", "stable"}
KNOWN_TOKENS = {
    "BTC", "ETH", "SOL", "SUI", "XRP", "DOGE", "LINK", "BNB",
    "USDC", "USDT", "PAXG", "HYPE", "ADA", "AVAX", "DOT", "MATIC",
    "SHIB", "UNI", "AAVE", "ARB", "OP", "NEAR", "FTM", "ATOM",
    "CASH", "USD", "STABLES", "STABLE",
}

class TRWRateLimitError(RuntimeError):
    """TRW login returned a rate-limit response (e.g. 'Too many requests')."""

    def __init__(self, message: str, retry_after_minutes: int | None = None):
        super().__init__(message)
        self.retry_after_minutes = retry_after_minutes


class TRWInvalidCredentialsError(RuntimeError):
    """TRW login rejected the supplied email/password."""


SESSION_DIR = os.path.join(os.path.dirname(__file__), "..", ".trw_session")
DEBUG_DIR = os.getenv("TRW_DEBUG_DIR", tempfile.gettempdir())
os.makedirs(DEBUG_DIR, exist_ok=True)
DEBUG_SCREENSHOT = os.path.join(DEBUG_DIR, "trw_debug.png")


def parse_signal(text: str) -> dict[str, float]:
    lower = text.lower()
    # Use rfind so correction messages that quote the original signal at the top
    # are parsed from the actual (last) "RSPS Signal:" section, not the quoted preview.
    start = lower.rfind(SIGNAL_MARKER)
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
    logger.info("[TRW] Navigating to login form...")
    # The landing page links to the login form on a separate route via an anchor
    # ("Log in to your account", href "/auth/login" -> redirects to /login/auth).
    # Earlier text locators matched a non-navigating "LOGIN" element and left us on
    # the landing page, where no input fields exist. Target the anchor by href and
    # wait for the form route so the email field is actually present.
    login_link = page.locator('a[href*="/auth/login"]')
    await login_link.first.wait_for(timeout=10000)
    await login_link.first.click()
    await page.wait_for_url("**/login/auth**", timeout=15000)

    logger.info("[TRW] Filling login form...")
    email_input = page.locator('input[type="email"], input[name="email"], input[placeholder*="mail"], input[placeholder*="Email"]')
    try:
        await email_input.first.wait_for(timeout=15000)
    except PwTimeout:
        try:
            await page.screenshot(path=DEBUG_SCREENSHOT, full_page=False)
            logger.error("[TRW] Login form did not appear — screenshot saved to %s", DEBUG_SCREENSHOT)
        except Exception:
            logger.exception("[TRW] Login form did not appear and screenshot capture failed")
        raise
    await email_input.first.fill(TRW_EMAIL)

    password_input = page.locator('input[type="password"]')
    await password_input.first.wait_for(timeout=5000)
    await password_input.first.fill(TRW_PASSWORD)

    # Target the button element specifically: the modal heading "Log In To The Real World"
    # also matches loose "Log In" text in DOM order, so a text-only locator clicks the heading.
    submit_btn = page.locator('button:has-text("Log In"), button[type="submit"]').first
    await submit_btn.click()
    await page.wait_for_timeout(5000)

    rate_limit_banner = page.locator("text=/Too many (requests|failed login)/i")
    if await rate_limit_banner.count() > 0 and await rate_limit_banner.first.is_visible():
        banner_text = (await rate_limit_banner.first.inner_text()).strip()
        await page.screenshot(path=DEBUG_SCREENSHOT, full_page=False)
        retry_match = re.search(r"(\d{1,4}) minute", banner_text, re.IGNORECASE)
        retry_after = int(retry_match.group(1)) if retry_match else None
        raise TRWRateLimitError(
            f"TRW login rate-limited: {banner_text}",
            retry_after_minutes=retry_after,
        )

    invalid_creds_banner = page.locator("text=/Invalid credentials/i")
    if await invalid_creds_banner.count() > 0 and await invalid_creds_banner.first.is_visible():
        banner_text = (await invalid_creds_banner.first.inner_text()).strip()
        await page.screenshot(path=DEBUG_SCREENSHOT, full_page=False)
        raise TRWInvalidCredentialsError(f"TRW login rejected: {banner_text}")

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

    # No button[type=submit] fallback: the hidden Log In form is still in DOM
    # behind the TOTP modal, so a generic submit selector would match the wrong button.
    confirm_btn = page.locator('button:has-text("Confirm")').first
    await confirm_btn.click(force=True)
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


def _normalize_timestamp(raw: str) -> str:
    """Convert TRW's relative timestamps into stable absolute strings.

    Examples:
        "Today at 3:09 AM"     -> "2026-04-18 03:09"
        "Yesterday at 11:30 PM" -> "2026-04-17 23:30"
        "04/07/2026"            -> "2026-04-07"
    """
    now = datetime.now()
    lower = raw.lower().strip()

    # "Today at 3:09 AM" / "Yesterday at 11:30 PM"
    m = re.match(r"(today|yesterday)\s+at\s+(.+)", lower)
    if m:
        day_label, time_str = m.group(1), m.group(2).strip()
        base = now if day_label == "today" else now - timedelta(days=1)
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                t = datetime.strptime(time_str, fmt)
                return base.strftime("%Y-%m-%d") + t.strftime(" %H:%M")
            except ValueError:
                continue
        return base.strftime("%Y-%m-%d") + f" {time_str}"

    # "04/07/2026" (MM/DD/YYYY)
    try:
        dt = datetime.strptime(raw.strip(), "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    return raw


async def _extract_timestamp(element) -> str | None:
    """Extract the posted-at timestamp from a TRW chat message element."""
    loc = element.locator("span.opacity-50")
    if await loc.count() > 0:
        text = (await loc.first.inner_text()).strip()
        if text:
            return _normalize_timestamp(text)
    return None


async def _message_body_text(element) -> str:
    """Return the message's own body text, excluding any reply-quote preview."""
    body = element.locator("span.custom-break-words")
    if await body.count() > 0:
        return await body.first.inner_text()
    return await element.inner_text()


async def _extract_signal(page) -> tuple[dict[str, float], str | None]:
    """Extract RSPS signal from the loaded channel page."""
    messages = page.locator('[class*="message"], [class*="chat"], [class*="post"]')
    count = await messages.count()
    logger.info("[TRW] Found %d message elements", count)

    signal_text = None
    signal_time = None
    signal_element = None
    for idx in range(count - 1, max(count - 20, -1), -1):
        element = messages.nth(idx)
        text = await _message_body_text(element)
        if SIGNAL_MARKER in text.lower():
            signal_text = text
            signal_element = element
            signal_time = await _extract_timestamp(signal_element)
            logger.info("[TRW] Found signal message at index %d (posted: %s)", idx, signal_time)
            break

    if not signal_text:
        body_text = await page.inner_text("body")
        if SIGNAL_MARKER in body_text.lower():
            start = body_text.lower().index(SIGNAL_MARKER)
            signal_text = body_text[start:]
            logger.info("[TRW] Found signal in page body text")

    if not signal_text:
        await page.screenshot(path=DEBUG_SCREENSHOT, full_page=False)
        raise RuntimeError(f"Could not find RSPS signal on page. Check {DEBUG_SCREENSHOT}")

    allocations = parse_signal(signal_text)
    if not allocations:
        raise RuntimeError(f"Found signal text but could not parse allocations:\n{signal_text[:300]}")

    logger.info("[TRW] Parsed allocations: %s", allocations)
    return allocations, signal_time


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
        await page.goto(TRW_SIGNAL_URL, wait_until="domcontentloaded", timeout=60000)
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
                await page.goto(TRW_SIGNAL_URL, wait_until="domcontentloaded", timeout=60000)
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
    await page.goto(TRW_SIGNAL_URL, wait_until="domcontentloaded", timeout=60000)
    await _login(page)

    # Handle device limit if it appears after login
    device_limit = page.get_by_text("Device Limit Reached", exact=False)
    if await device_limit.count() > 0:
        await _handle_device_limit(page)
        await page.goto(TRW_SIGNAL_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

    # Save session for reuse
    if save_session:
        os.makedirs(session_path, exist_ok=True)
        await context.storage_state(path=state_file)
        logger.info("[TRW] Session saved to %s", session_path)

    return browser, context, page


async def _jump_to_latest(page) -> None:
    """Click 'Viewing older messages' banner if present to jump to the latest messages."""
    btn = page.get_by_text("Viewing older messages", exact=False)
    if await btn.count() > 0:
        logger.info("[TRW] 'Viewing older messages' banner found — clicking to jump to latest")
        # Chat input overlay can intercept pointer events; force the click past it.
        await btn.first.click(force=True)
        await page.wait_for_timeout(5000)
    else:
        logger.info("[TRW] Already viewing latest messages")


async def fetch_signal() -> tuple[dict[str, float], str | None]:
    if not all([TRW_EMAIL, TRW_PASSWORD, TRW_TOTP_SECRET]):
        raise ValueError("TRW_EMAIL, TRW_PASSWORD, and TRW_TOTP_SECRET must be set in .env")

    browser = None
    page = None
    async with async_playwright() as p:
        try:
            browser, context, page = await _open_channel(p)
            await page.wait_for_timeout(5000)

            await _jump_to_latest(page)

            allocations, signal_time = await _extract_signal(page)
            return allocations, signal_time
        except PwTimeout as err:
            saved = False
            if page is not None:
                try:
                    await page.screenshot(path=DEBUG_SCREENSHOT, full_page=False)
                    saved = True
                except Exception:
                    logger.exception("[TRW] Failed to save debug screenshot")
            if saved:
                logger.error("[TRW] Timeout — screenshot saved to %s", DEBUG_SCREENSHOT)
            else:
                logger.error("[TRW] Timeout before page was ready — no screenshot captured (%s)", err)
            raise RuntimeError(f"TRW page timed out: {err}") from err
        finally:
            if browser:
                await browser.close()
