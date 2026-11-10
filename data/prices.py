import logging
import requests
from requests import RequestException

import constants
from constants import COINGECKO_IDS, CRYPTO_PRICES_URL
from data.hyperliquid_account import THOUSAND_PREFIX


logger = logging.getLogger(__name__)


class PriceFetchError(RuntimeError):
    pass


class PriceRateLimitError(PriceFetchError):
    """CoinGecko returned HTTP 429."""


# Public market data, no credentials. Tokens a signal adds beyond COINGECKO_IDS are
# only tracked when Binance lists them against USDC, so that pair is also where
# their price comes from — the same market the bot would trade them on.
BINANCE_API_URL = "https://api.binance.com/api/v3"
BINANCE_QUOTE = "USDC"
INVALID_SYMBOL = -1121


def binance_lists_spot_pair(base: str, quote: str = BINANCE_QUOTE) -> bool | None:
    """Whether Binance spot trades base/quote right now; None when Binance could not say."""
    try:
        response = requests.get(f"{BINANCE_API_URL}/exchangeInfo",
                                params={"symbol": f"{base}{quote}"}, timeout=10)
        payload = response.json()
    except (RequestException, ValueError) as error:
        logger.warning("Binance listing check for %s/%s failed: %s", base, quote, error)
        return None
    if response.status_code == 400 and payload.get("code") == INVALID_SYMBOL:
        return False
    if response.status_code != 200:
        logger.warning("Binance listing check for %s/%s returned HTTP %s: %s",
                       base, quote, response.status_code, payload)
        return None
    markets = payload.get("symbols") or []
    return any(m.get("status") == "TRADING" and m.get("isSpotTradingAllowed") for m in markets)


HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"


def _hyperliquid_mids() -> dict[str, str]:
    """Mid price of every Hyperliquid market, perps keyed by coin name."""
    response = requests.post(HYPERLIQUID_INFO_URL, json={"type": "allMids"}, timeout=10)
    response.raise_for_status()
    return response.json()


def _perp_price(mids: dict, symbol: str) -> float | None:
    """USD price of one `symbol` off its perp, scaling a per-thousand perp down."""
    if symbol in mids:
        return float(mids[symbol])
    if f"{THOUSAND_PREFIX}{symbol}" in mids:
        return float(mids[f"{THOUSAND_PREFIX}{symbol}"]) / 1_000
    return None


def hyperliquid_lists(symbol: str) -> bool | None:
    """Whether Hyperliquid lists a perp on `symbol`; None when it could not say."""
    try:
        return _perp_price(_hyperliquid_mids(), symbol) is not None
    except (RequestException, ValueError) as error:
        logger.warning("Hyperliquid listing check for %s failed: %s", symbol, error)
        return None


def _fetch_hyperliquid_prices(symbols: list[str]) -> dict[str, float]:
    """USD prices off Hyperliquid's perp mids, the venue the book is traded on.

    A perp's mid is used for its spot token too: the two track each other within a
    few basis points, and every coin the bot holds has a perp but not a spot book.
    """
    try:
        mids = _hyperliquid_mids()
    except (RequestException, ValueError) as error:
        logger.exception("Error fetching Hyperliquid prices")
        raise PriceFetchError(f"Hyperliquid price request failed: {error}") from error
    prices = {}
    for symbol in symbols:
        price = 1.0 if symbol == "USDC" else _perp_price(mids, symbol)
        if price is not None:
            prices[symbol] = price
    return prices


def _fetch_coingecko_prices(symbols: list[str]) -> dict[str, float]:
    ids_to_symbol = {COINGECKO_IDS[s]: s for s in symbols}
    try:
        response = requests.get(
            CRYPTO_PRICES_URL,
            params={"ids": ",".join(ids_to_symbol), "vs_currencies": "usd"},
            timeout=15,
        )
        response.raise_for_status()
    except RequestException as error:
        logger.exception("Error fetching prices: %s", error)
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status == 429:
            raise PriceRateLimitError(f"CoinGecko rate-limited: {error}") from error
        raise PriceFetchError(f"price API request failed: {error}") from error

    prices = {}
    for coin_id, values in response.json().items():
        if "usd" not in values:
            logger.warning("Price for %s not found", coin_id)
            continue
        prices[ids_to_symbol[coin_id]] = values["usd"]
    return prices


def _fetch_binance_prices(symbols: list[str]) -> dict[str, float]:
    """USD prices read off Binance's <symbol>USDC pairs, for tokens CoinGecko is not mapped for."""
    pairs = {f"{s}{BINANCE_QUOTE}": s for s in symbols}
    try:
        response = requests.get(
            f"{BINANCE_API_URL}/ticker/price",
            params={"symbols": "[" + ",".join(f'"{pair}"' for pair in pairs) + "]"},
            timeout=15,
        )
        response.raise_for_status()
        tickers = response.json()
    except (RequestException, ValueError) as error:
        logger.exception("Error fetching Binance prices for %s", ", ".join(symbols))
        raise PriceFetchError(f"no price source for: {', '.join(symbols)} ({error})") from error
    try:
        return {pairs[t["symbol"]]: float(t["price"]) for t in tickers if t.get("symbol") in pairs}
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        # A ticker without a price, a non-numeric one, or an error object instead of the list.
        raise PriceFetchError(f"malformed Binance ticker response: {tickers!r:.200}") from error


def fetch_prices(symbols: list) -> dict[str, float]:
    wanted = [s.upper() for s in symbols]
    if constants.on_hyperliquid():
        prices = _fetch_hyperliquid_prices(wanted)
    else:
        mapped = [s for s in wanted if s in COINGECKO_IDS]
        unmapped = [s for s in wanted if s not in COINGECKO_IDS]
        prices = _fetch_coingecko_prices(mapped) if mapped else {}
        if unmapped:
            prices.update(_fetch_binance_prices(unmapped))

    missing = [s for s in wanted if s not in prices]
    if missing:
        raise PriceFetchError(f"missing prices for: {', '.join(missing)}")

    logger.info("Fetched prices for %d symbols", len(prices))
    return prices
