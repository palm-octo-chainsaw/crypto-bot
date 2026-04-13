import logging
import requests
from requests import RequestException

from constants import CRYPTO_PRICES_URL


logger = logging.getLogger(__name__)


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
        return {}

    prices = {}
    for symbol, values in response.json().items():
        if "usd" not in values:
            logger.warning("Price for %s not found", symbol)
            continue
        prices[symbol.upper()] = values["usd"]

    logger.info("Fetched prices for %d symbols", len(prices))
    return prices
