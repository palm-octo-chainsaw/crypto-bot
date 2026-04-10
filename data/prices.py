import logging
from typing import Dict
import requests
from requests import RequestException

from constants import CRYPTO_PRICES_URL


logger = logging.getLogger(__name__)


def fetch_prices(symbols: list) -> Dict[str, float]:
    try:
        params = {
            "symbols": ", ".join(symbols),
            "vs_currencies": "usd"
        }
        response = requests.get(CRYPTO_PRICES_URL, params=params, timeout=15)
        response.raise_for_status()

        logger.info("Prices fetched successfully: %s", response)
        data = response.json()

        prices: dict = {}
        for key, value in data.items():
            if 'usd' not in value:
                logger.warning("Price for %s not found", key)
                continue
            prices[key.upper()] = value['usd']

        return prices

    except RequestException as error:
        logger.error("Error fetching prices: %s", error)
        return {}
