"""The currency values are shown in.

The bot counts in USD throughout: prices, snapshots and trades are all stored that
way. Only what reaches the chat is converted, at the ECB's daily reference rate.
"""

import logging
import time

import requests
from requests import RequestException

import constants
from constants import FX_RATES_URL
from data.database import DisplayRate, get_setting, set_setting


logger = logging.getLogger(__name__)

BASE = "USD"
SETTING_KEY = "display_currency"
# ECB reference rates move once a working day. Within the hour every line of a
# message is converted at one rate, without a request per line.
RATE_TTL_SECONDS = 3600
# After a failed fetch, wait this long before trying again: one dead API must not
# cost every money line of a message its own timeout.
RETRY_AFTER_SECONDS = 300
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}

_rates: dict[str, tuple[float, float]] = {}
_failed_at: dict[str, float] = {}
_selected: str | None = None


class UnknownCurrency(Exception):
    """The rate source does not know this currency code."""


class FxUnavailable(RuntimeError):
    """No rate could be had for a known currency."""


def _fetch_rate(code: str) -> float:
    try:
        response = requests.get(FX_RATES_URL, params={"base": BASE, "symbols": code}, timeout=10)
    except RequestException as error:
        raise FxUnavailable(f"FX rate request for {code} failed: {error}") from error
    # 404 for a well-formed code it has no rate for, 422 for a malformed one.
    if response.status_code in (404, 422):
        raise UnknownCurrency(code)
    try:
        response.raise_for_status()
        return float(response.json()["rates"][code])
    except (RequestException, KeyError, TypeError, ValueError) as error:
        raise FxUnavailable(f"no FX rate for {code}: {error}") from error


def usd_rate(code: str) -> float:
    """Units of `code` one USD buys, or the last rate fetched when a refresh fails."""
    if code == BASE:
        return 1.0
    now = time.monotonic()
    cached = _rates.get(code)
    if cached and now - cached[1] < RATE_TTL_SECONDS:
        return cached[0]
    failed = _failed_at.get(code)
    if failed is not None and now - failed < RETRY_AFTER_SECONDS:
        if cached:
            return cached[0]
        raise FxUnavailable(f"no FX rate for {code} (last fetch failed)")
    try:
        rate = _fetch_rate(code)
    except FxUnavailable:
        _failed_at[code] = now
        if cached:
            logger.warning("FX rate for %s unavailable, keeping the last one", code)
            return cached[0]
        raise
    _failed_at.pop(code, None)
    _rates[code] = (rate, now)
    return rate


def current() -> str:
    """The display currency: the one /currency chose, else DISPLAY_CURRENCY."""
    global _selected
    if _selected is None:
        try:
            _selected = get_setting(SETTING_KEY) or constants.DISPLAY_CURRENCY
        except Exception as error:
            # Not cached, so the stored choice is picked up once the database answers.
            logger.warning("Could not read the display currency, using %s: %s",
                           constants.DISPLAY_CURRENCY, error)
            return constants.DISPLAY_CURRENCY
    return _selected


def select(code: str) -> float:
    """Make `code` the display currency and return its rate.

    Raises UnknownCurrency or FxUnavailable, leaving the current one in place:
    a currency is only stored once a rate for it has been seen.
    """
    global _selected
    code = code.strip().upper()
    rate = usd_rate(code)
    set_setting(SETTING_KEY, code)
    _selected = code
    return rate


def rate_now() -> DisplayRate:
    """The display currency and its rate, to store beside a USD amount being recorded.

    Empty when no rate can be had: a trade or snapshot is recorded either
    way, and is shown at the rate of the day it is read instead.
    """
    code = current()
    try:
        return DisplayRate(code, usd_rate(code))
    except (UnknownCurrency, FxUnavailable) as error:
        logger.warning("Recording without a %s rate: %s", code, error)
        return DisplayRate()


def money(usd: float, at: DisplayRate | None = None) -> str:
    """A USD amount in the display currency, e.g. €1,234.56; in USD when no rate can be had.

    `at` is the (currency, rate) recorded with a past amount. It is used when that
    is still the display currency, so the amount reads as it did on its day; any
    other display currency converts at today's rate.
    """
    code = current()
    at = at or DisplayRate()
    try:
        rate = at.rate if at.currency == code and at.rate else usd_rate(code)
    except (UnknownCurrency, FxUnavailable) as error:
        logger.warning("Showing USD instead of %s: %s", code, error)
        code, rate = BASE, 1.0
    value = usd * rate
    sign = "-" if value < 0 else ""
    symbol = SYMBOLS.get(code)
    if symbol:
        return f"{sign}{symbol}{abs(value):,.2f}"
    return f"{sign}{abs(value):,.2f} {code}"
