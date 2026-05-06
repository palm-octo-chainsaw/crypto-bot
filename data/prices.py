import logging
import requests
from requests import RequestException

from constants import CRYPTO_PRICES_URL


logger = logging.getLogger(__name__)


class PriceFetchError(RuntimeError):
    pass


class PriceRateLimitError(PriceFetchError):
    """CoinGecko returned HTTP 429."""


def fetch_prices(symbols: list) -> dict[str, float]:
    try:
        response = requests.get(
            CRYPTO_PRICES_URL,
            params={"symbols": ", ".join(symbols), "vs_currencies": "usd"},
            timeout=15,
        )
        response.raise_for_status()
    except RequestException as error:
        logger.error("Error fetching prices: %s", error)
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status == 429:
            raise PriceRateLimitError(f"CoinGecko rate-limited: {error}") from error
        raise PriceFetchError(f"price API request failed: {error}") from error

    prices = {}
    for symbol, values in response.json().items():
        if "usd" not in values:
            logger.warning("Price for %s not found", symbol)
            continue
        prices[symbol.upper()] = values["usd"]

    missing = [s for s in symbols if s.upper() not in prices]
    if missing:
        raise PriceFetchError(f"missing prices for: {', '.join(missing)}")

    logger.info("Fetched prices for %d symbols", len(prices))
    return prices
