"""Scrape RSPS signal allocations from The Real World."""

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from itertools import groupby
from playwright.async_api import async_playwright, TimeoutError as PwTimeout
import pyotp

from constants import TRW_EMAIL, TRW_PASSWORD, TRW_TOTP_SECRET, TRW_SIGNAL_URL

logger = logging.getLogger(__name__)

SIGNAL_MARKER = "rsps signal"
# A sign attached to the number ("-10% SOL") marks a short; a bullet ("- 40% BTC")
# is kept apart from it by its space. A direction word between the percentage and
# the coin ("10% Short SOL") is skipped, so the coin is read rather than the word.
ALLOCATION_LINE = re.compile(
    r"(?P<sign>(?<![\w.])[-−])?(?P<pct>\d*\.?\d+)%\s+"
    r"(?:(?:long|short)(?:s|ed|ing)?\s+)?(?P<symbol>\w+)",
    re.IGNORECASE,
)
# "short-term" describes a horizon, not a direction.
SHORT_WORD = re.compile(r"\bshort(?:s|ed|ing)?\b(?!-term)", re.IGNORECASE)
LONG_WORD = re.compile(r"\blong(?:s|ed|ing)?\b", re.IGNORECASE)
DIRECTION_WORD = re.compile(r"(?:long|short)(?:s|ed|ing)?", re.IGNORECASE)
# A line that is only a heading ("Shorts:", "🔻 Short positions") sets the side of
# the allocations below it, up to the next such heading.
DIRECTION_HEADER = re.compile(
    r"^[^\w\n]*(?P<side>long|short)s?(?:[ \t]+(?:positions?|allocations?|side))?[^\w\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
# The signal's combined book. The modules above it list the same positions again,
# so reading them too would count each one twice.
NET_POSITIONS_HEADER = re.compile(r"^[^\w\n]*net[ \t]+positions?\b", re.IGNORECASE | re.MULTILINE)
# Longs adding up to this range are the whole book, so shorts listed beside them
# are paid for out of its USDC rather than added on top.
FULL_BOOK = (95.0, 105.0)
STABLE = "USDC"
CASH_ALIASES = {"cash", "usd", "stables", "stable"}
KNOWN_TOKENS = {
    "BTC", "ETH", "SOL", "SUI", "XRP", "DOGE", "LINK", "BNB",
    "USDC", "USDT", "PAXG", "HYPE", "ADA", "AVAX", "DOT", "MATIC",
    "SHIB", "UNI", "AAVE", "ARB", "OP", "NEAR", "FTM", "ATOM",
    "CASH", "USD", "STABLES", "STABLE",
}
# A token outside KNOWN_TOKENS is kept when written like a ticker, so a new coin in
# a signal is not dropped before Binance is asked whether it trades it. Requiring
# capitals keeps prose ("10% of the book") out, since the line pattern ignores case.
UNLISTED_TICKER = re.compile(r"(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,10}")

class TRWRateLimitError(RuntimeError):
    """TRW login returned a rate-limit response (e.g. 'Too many requests')."""

    def __init__(self, message: str, retry_after_minutes: int | None = None):
        super().__init__(message)
        self.retry_after_minutes = retry_after_minutes


class TRWInvalidCredentialsError(RuntimeError):
    """TRW login rejected the supplied email/password."""


class AmbiguousSignalError(ValueError):
    """The signal's text does not say which way a position goes.

    Raised rather than guessed: reading a short as a long buys the coin the signal
    says to bet against.
    """


SESSION_DIR = os.path.join(os.path.dirname(__file__), "..", ".trw_session")
DEBUG_DIR = os.getenv("TRW_DEBUG_DIR", tempfile.gettempdir())
os.makedirs(DEBUG_DIR, exist_ok=True)
DEBUG_SCREENSHOT = os.path.join(DEBUG_DIR, "trw_debug.png")
BANNER_WAIT_MS = 20000
# TRW renders message times in the browser's timezone and dates in its locale.
# Pinning both keeps "Today at 3:09 AM" and "04/07/2026" parseable as UTC,
# whatever the host is set to.
BROWSER_CONTEXT = {
    "viewport": {"width": 1280, "height": 900},
    "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "timezone_id": "UTC",
    "locale": "en-US",
}
# The scheduled poll and a manual /signal can scrape at once; both read and write
# the saved session, so a read could load a half-written state.json.
_SESSION_LOCK = asyncio.Lock()


def _allocation_symbol(match: re.Match) -> str | None:
    """The coin an allocation names, or None when the word after the % is not one."""
    raw = match.group("symbol")
    if DIRECTION_WORD.fullmatch(raw):
        # "10% SHORT" with no coin: dropping it would scale the rest up into its room.
        raise AmbiguousSignalError(f"Allocation names no coin: {match.group(0)!r}")
    symbol = raw.upper()
    if symbol not in KNOWN_TOKENS:
        if not UNLISTED_TICKER.fullmatch(raw):
            return None
        logger.info("Signal names %s, outside the known tokens — kept as a ticker", symbol)
    return STABLE if symbol.lower() in CASH_ALIASES else symbol


def _line_bounds(text: str, match: re.Match) -> tuple[int, int]:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    return start, len(text) if end == -1 else end


def _in_short_section(headers: list[tuple[int, bool]], pos: int) -> bool:
    """Whether the nearest direction heading above `pos` is a shorts heading."""
    short = False
    for start, is_short in headers:
        if start >= pos:
            break
        short = is_short
    return short


def _short_flags(line: str, line_start: int, matches: list[re.Match],
                 short_section: bool) -> list[bool]:
    """Which of one line's allocations are shorts.

    Every unclear case resolves toward short or refuses the signal, never toward
    long: a short read as a long buys the coin the signal bets against. A sign on
    the number, or a direction word inside the allocation ("10% Short SOL"), binds
    to that allocation. A word outside them ("10% SOL (SHORT)") binds to the line's
    only allocation, or to all of them when it opens the line ("Shorts: 10% SOL,
    5% ETH"). Anywhere else it is ambiguous.
    """
    if short_section:
        return [True] * len(matches)
    flags = [bool(m.group("sign") or SHORT_WORD.search(m.group(0))) for m in matches]
    rest = list(line)
    for m in matches:
        rest[m.start() - line_start:m.end() - line_start] = " " * (m.end() - m.start())
    rest = "".join(rest)
    if not SHORT_WORD.search(rest):
        return flags
    if len(matches) == 1:
        return [True]
    opening = matches[0].start() - line_start
    if not SHORT_WORD.search(rest, opening) and not LONG_WORD.search(rest):
        return [True] * len(matches)
    raise AmbiguousSignalError(f"Can't tell which allocations are short in: {line.strip()!r}")


def parse_signal(text: str) -> dict[str, float]:
    """Read a signal's allocations as signed weights: a short is negative.

    Raises AmbiguousSignalError when the text does not say which way a position
    goes, or names one coin on both sides.
    """
    section = _signal_section(text)
    if section is None:
        return {}

    longs, shorts = _collect_allocations(section)
    both = sorted(longs.keys() & shorts.keys())
    if both:
        raise AmbiguousSignalError(f"Signal lists {', '.join(both)} as both long and short")
    _fund_shorts_from_cash(longs, shorts)

    # Gross: a short's weight is part of the book, so leaving it out of the total
    # would scale the longs up into the room it takes.
    total_allocation = sum(longs.values()) + sum(shorts.values())
    if total_allocation == 0:
        logger.warning("Parsed allocations sum to 0%% — returning empty")
        return {}

    factor = 1.0
    if not (FULL_BOOK[0] <= total_allocation <= FULL_BOOK[1]):
        logger.warning(
            "Parsed allocations sum to %.2f%% (outside [95%%, 105%%]); normalizing to 100%%",
            total_allocation,
        )
        factor = 100.0 / total_allocation

    if shorts:
        logger.info("Signal shorts %s", ", ".join(f"{s} {pct:.2f}%" for s, pct in shorts.items()))
    allocations = {symbol: pct * factor for symbol, pct in longs.items()}
    allocations.update({symbol: -pct * factor if pct else 0.0 for symbol, pct in shorts.items()})
    return allocations


def _fund_shorts_from_cash(longs: dict[str, float], shorts: dict[str, float]) -> None:
    """Take the shorts out of the USDC when the longs alone already fill the book.

    "100% USDC" with 12% of shorts beside it means 88% USDC and 12% short, not a
    112% book. Shorts that complete the longs to 100% ("90% BTC", "10% SOL short")
    are already inside the book and are left alone.
    """
    short_total = sum(shorts.values())
    if not short_total or not (FULL_BOOK[0] <= sum(longs.values()) <= FULL_BOOK[1]):
        return
    cash = longs.get(STABLE, 0.0)
    if cash < short_total:
        raise AmbiguousSignalError(
            f"Signal's shorts ({short_total:.2f}%) exceed its {STABLE} ({cash:.2f}%) "
            "on a book the longs already fill"
        )
    logger.info("Shorts %.2f%% taken out of %s %.2f%%", short_total, STABLE, cash)
    longs[STABLE] = cash - short_total


def _signal_section(text: str) -> str | None:
    # Use rfind so correction messages that quote the original signal at the top
    # are parsed from the actual (last) "RSPS Signal:" section, not the quoted preview.
    start = text.lower().rfind(SIGNAL_MARKER)
    if start == -1:
        return None

    section = text[start:]
    for delimiter in ["Executive Summary", "Associated Data", "———", "─────"]:
        end = section.find(delimiter)
        if end > 0:
            section = section[:end]
            break
    net = NET_POSITIONS_HEADER.search(section)
    return section[net.start():] if net else section


def _collect_allocations(section: str) -> tuple[dict[str, float], dict[str, float]]:
    """Sum the section's allocations into (longs, shorts), both as positive weights."""
    headers = [(m.start(), m.group("side").lower() == "short")
               for m in DIRECTION_HEADER.finditer(section)]
    parsed = [(match, symbol) for match in ALLOCATION_LINE.finditer(section)
              if (symbol := _allocation_symbol(match))]
    books = {False: {}, True: {}}  # keyed by is_short
    for (line_start, line_end), group in groupby(
            parsed, key=lambda item: _line_bounds(section, item[0])):
        group = list(group)
        flags = _short_flags(section[line_start:line_end], line_start,
                             [match for match, _ in group],
                             _in_short_section(headers, line_start))
        for (match, symbol), is_short in zip(group, flags):
            _add_allocation(books, match, symbol, is_short)
    return books[False], books[True]


def _add_allocation(books: dict[bool, dict[str, float]], match: re.Match,
                    symbol: str, is_short: bool) -> None:
    if is_short and symbol == STABLE:
        logger.warning("Signal shorts cash (%r) — read as a cash allocation", match.group(0))
        is_short = False
    book = books[is_short]
    pct = float(match.group("pct"))
    if symbol in book:
        logger.info("Duplicate allocation for %s — aggregating %.2f + %.2f", symbol, book[symbol], pct)
    book[symbol] = book.get(symbol, 0.0) + pct


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


async def _device_limit_showing(page) -> bool:
    return await page.get_by_text("Device Limit Reached", exact=False).count() > 0


async def _handle_device_limit(page) -> bool:
    """Dismiss device-limit modal by logging out the oldest non-current sessions.

    Returns whether the modal is gone afterwards.
    """
    logger.info("[TRW] Device limit modal detected — removing old sessions...")

    for _ in range(5):
        if not await _device_limit_showing(page):
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
        except Exception as err:
            logger.warning("[TRW] Device limit close button click failed: %s", err)
    await page.wait_for_timeout(2000)

    if await _device_limit_showing(page):
        logger.warning("[TRW] Device limit modal still showing after cleanup")
        return False
    logger.info("[TRW] Device limit resolved")
    return True


async def _clear_device_limit(page) -> None:
    """Clear the device-limit modal if it is up, and reload the channel behind it.

    Raises when the modal survives the reload: extraction would otherwise find
    no messages and fail with an error that hides the cause, and a fresh login
    would save the blocked session for the next run to reuse.
    """
    if not await _device_limit_showing(page):
        return
    await _handle_device_limit(page)
    await page.goto(TRW_SIGNAL_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    if await _device_limit_showing(page):
        await page.screenshot(path=DEBUG_SCREENSHOT, full_page=False)
        raise RuntimeError(f"TRW device limit modal would not clear. Check {DEBUG_SCREENSHOT}")


def _normalize_timestamp(raw: str) -> str:
    """Convert TRW's relative timestamps into stable absolute strings.

    Examples:
        "Today at 3:09 AM"     -> "2026-04-18 03:09"
        "Yesterday at 11:30 PM" -> "2026-04-17 23:30"
        "04/07/2026"            -> "2026-04-07"

    Relies on the browser rendering in UTC and en-US (see BROWSER_CONTEXT).
    """
    now = datetime.now(timezone.utc)
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
    """Return the message's own body text, excluding any reply-quote preview.

    The message selector can match non-HTML nodes (e.g. SVG icons whose class
    contains "chat"/"message"), and ``inner_text`` raises "Node is not an
    HTMLElement" on those. Treat such nodes as empty so the scan continues to
    real messages instead of aborting the whole extraction.

    Any other failure propagates: skipping a real message whose text could not
    be read would let the scan walk on to an older, stale signal.
    """
    body = element.locator("span.custom-break-words")
    try:
        if await body.count() > 0:
            return await body.first.inner_text()
        return await element.inner_text()
    except Exception as err:
        if "not an HTMLElement" in str(err):
            return ""
        raise


async def _extract_signal(page) -> tuple[dict[str, float], str | None]:
    """Extract RSPS signal from the loaded channel page."""
    messages = page.locator('[class*="message"], [class*="chat"], [class*="post"]')
    count = await messages.count()
    logger.info("[TRW] Found %d message elements", count)

    signal_text = None
    signal_time = None
    signal_element = None
    # The newest 20 messages; range()'s stop is exclusive.
    for idx in range(count - 1, max(count - 21, -1), -1):
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


async def _is_logged_out(page) -> bool:
    """Return True if the page is showing the login screen instead of the chat.

    A valid session keeps us on the /chat/ route. When the session expires, TRW
    redirects to the login form. We treat any of these as logged-out:
      - the URL is no longer on the /chat/ route
      - the "Log In To The Real World" heading is present
      - a password input is visible
    """
    if "/chat/" not in page.url:
        return True

    login_heading = page.get_by_text("Log In To The Real World", exact=False)
    if await login_heading.count() > 0:
        return True

    password_input = page.locator('input[type="password"]')
    if await password_input.count() > 0 and await password_input.first.is_visible():
        return True

    return False


async def _open_channel(p, *, save_session: bool = True):
    """Open signal channel, handling login and device limits. Returns (browser, context, page).

    The browser is the caller's to close only once this returns; on any failure
    it is closed here, since the caller never receives it.
    """
    session_path = os.path.abspath(SESSION_DIR)
    state_file = os.path.join(session_path, "state.json")

    browser = None
    try:
        # Try reusing saved session first
        if os.path.isfile(state_file):
            logger.info("[TRW] Reusing saved session...")
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=state_file, **BROWSER_CONTEXT)
            page = await context.new_page()
            await page.goto(TRW_SIGNAL_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # Check if we're still logged in. A live session stays on the /chat/ route;
            # an expired one redirects to the login form ("Log In To The Real World" with
            # email/password inputs). Detect via several signals rather than one brittle
            # text match — the prior "LOGIN TO YOUR ACCOUNT" string no longer matches the
            # current login page, which left expired sessions undetected (0 messages found).
            if await _is_logged_out(page):
                logger.info("[TRW] Session expired, logging in again...")
                await browser.close()
                browser = None
            else:
                await _clear_device_limit(page)
                return browser, context, page

        # Fresh login
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(**BROWSER_CONTEXT)
        page = await context.new_page()
        await page.goto(TRW_SIGNAL_URL, wait_until="domcontentloaded", timeout=60000)
        await _login(page)

        await _clear_device_limit(page)

        # Save session for reuse
        if save_session:
            os.makedirs(session_path, exist_ok=True)
            await context.storage_state(path=state_file)
            logger.info("[TRW] Session saved to %s", session_path)

        return browser, context, page
    except BaseException:
        if browser is not None:
            await browser.close()
        raise


async def _jump_to_latest(page) -> None:
    """Click 'Viewing older messages' banner if present to jump to the latest messages.

    The channel can open on old history before the banner renders. A one-shot
    check on a slow load finds no banner, reads the old history as the latest,
    and returns a stale signal, so wait for the banner before concluding.
    """
    btn = page.get_by_text("Viewing older messages", exact=False).first
    try:
        await btn.wait_for(state="visible", timeout=BANNER_WAIT_MS)
    except PwTimeout:
        logger.info("[TRW] No 'Viewing older messages' banner after %ds — assuming latest",
                    BANNER_WAIT_MS // 1000)
        return
    logger.info("[TRW] 'Viewing older messages' banner found — clicking to jump to latest")
    # Chat input overlay can intercept pointer events; force the click past it.
    await btn.click(force=True)
    await page.wait_for_timeout(5000)


async def fetch_signal() -> tuple[dict[str, float], str | None]:
    if not all([TRW_EMAIL, TRW_PASSWORD, TRW_TOTP_SECRET]):
        raise ValueError("TRW_EMAIL, TRW_PASSWORD, and TRW_TOTP_SECRET must be set in .env")

    async with _SESSION_LOCK:
        return await _fetch_signal()


async def _fetch_signal() -> tuple[dict[str, float], str | None]:
    browser = None
    page = None
    async with async_playwright() as p:
        try:
            browser, _, page = await _open_channel(p)
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
                logger.exception("[TRW] Timeout before page was ready — no screenshot captured (%s)", err)
            raise RuntimeError(f"TRW page timed out: {err}") from err
        finally:
            if browser:
                await browser.close()
