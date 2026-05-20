import logging
import requests
import krakenex
from web3 import Web3
from binance.client import Client

from constants import (
    META_MASK,
    BINANCE_API_KEY, BINANCE_API_SECRET,
    KRAKEN_API_KEY, KRAKEN_API_SECRET,
)

logger = logging.getLogger(__name__)


class Balance:
    ARBITRUM_RPC = "https://arbitrum-one-rpc.publicnode.com"

    ERC20_ABI = [
        {
            "constant": True,
            "type": "function",
            "name": "balanceOf",
            "inputs": [{"name": "_owner", "type": "address"}],
            "outputs": [{"name": "balance", "type": "uint256"}],
        },
        {
            "constant": True,
            "type": "function",
            "name": "decimals",
            "inputs": [],
            "outputs": [{"name": "", "type": "uint8"}],
        },
        {
            "constant": True,
            "type": "function",
            "name": "symbol",
            "inputs": [],
            "outputs": [{"name": "", "type": "string"}],
        },
    ]

    KRAKEN_SYMBOL_MAP = {
        "BTC":  "XXBT",
        "ETH":  "XETH",
        "SOL":  "SOL",
        "XRP":  "XXRP",
        "DOGE": "XDG",
        "USDC": "USDC",
        "LINK": "LINK",
        "PAXG": "PAXG",
    }

    USDC_CONTRACT_ADDRESS = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"

    LEVERAGE_TOKENS = {
        "BTCBULL2X": "0xe3254397f5D9C0B69917EBb49B49e103367B406f",
        "BTCBULL4X": "0xd49d22f2a2f05B2088fD42503409E430a8a7D827",
        "ETHBULL4X": "0xBf4aB4224B2AC26667Cd4b8A0E5134D55cB0B293",
    }

    def __init__(self):
        self.binance_client = None
        if BINANCE_API_KEY and BINANCE_API_SECRET:
            self.binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        else:
            logger.warning("Binance API credentials missing; Binance balances will not be fetched.")

        self.kraken_client = None
        if KRAKEN_API_KEY and KRAKEN_API_SECRET:
            self.kraken_client = krakenex.API(key=KRAKEN_API_KEY, secret=KRAKEN_API_SECRET)
        else:
            logger.warning("Kraken API credentials missing; Kraken balances will not be fetched.")

        self._binance_balances: dict | None = None
        self._w3: Web3 | None = None
        self._contracts: dict = {}

    @property
    def w3(self) -> Web3:
        if self._w3 is None or not self._w3.is_connected():
            self._w3 = Web3(Web3.HTTPProvider(self.ARBITRUM_RPC))
            if not self._w3.is_connected():
                logger.warning("Unable to connect to Arbitrum RPC at %s", self.ARBITRUM_RPC)
        return self._w3

    def _kraken_balance(self, symbol: str, kraken_raw: dict) -> float:
        kraken_key = self.KRAKEN_SYMBOL_MAP.get(symbol)
        if kraken_key is None:
            return 0.0
        return float(kraken_raw.get(kraken_key, 0.0))

    def get_spot_balance(self) -> dict:
        kraken_raw = self.get_raw_kraken_balance()
        hl = self.get_hyperliquid_balances()

        return {
            "BTC":  self.get_binance_balance("BTC") + self._kraken_balance("BTC", kraken_raw),
            "PAXG": self._kraken_balance("PAXG", kraken_raw),
            "SOL":  self.get_binance_balance("SOL") + self._kraken_balance("SOL", kraken_raw),
            "SUI":  self.get_binance_balance("SUI"),
            "USDC": self.get_usdc_balance() + self._kraken_balance("USDC", kraken_raw) + hl.get("USDC", 0.0),
            "ETH":  self.get_eth_balance() + self._kraken_balance("ETH", kraken_raw),
            "DOGE": self.get_binance_balance("DOGE") + self._kraken_balance("DOGE", kraken_raw),
            "XRP":  self.get_binance_balance("XRP") + self._kraken_balance("XRP", kraken_raw),
            "LINK": self.get_binance_balance("LINK") + self._kraken_balance("LINK", kraken_raw),
            "HYPE": hl.get("HYPE", 0.0),
            "BNB":  self.get_binance_balance("BNB"),
        }

    def get_leverage_balance(self) -> dict:
        return {
            name: self._get_erc20_balance(address)
            for name, address in self.LEVERAGE_TOKENS.items()
        }

    def _get_contract(self, token_contract: str):
        checksum = Web3.to_checksum_address(token_contract)
        if checksum not in self._contracts:
            self._contracts[checksum] = self.w3.eth.contract(address=checksum, abi=self.ERC20_ABI)
        return self._contracts[checksum]

    def _get_erc20_balance(self, token_contract: str) -> float:
        try:
            contract = self._get_contract(token_contract)
            balance = contract.functions.balanceOf(Web3.to_checksum_address(META_MASK)).call()
            decimals = contract.functions.decimals().call()
            return balance / (10 ** decimals)
        except Exception:
            logger.error(
                "Error fetching token balance for contract %s",
                token_contract,
                exc_info=True,
            )
            return 0.0

    def get_usdc_balance(self) -> float:
        return self._get_erc20_balance(self.USDC_CONTRACT_ADDRESS) + self.get_binance_balance("USDC")

    def get_eth_balance(self) -> float:
        try:
            balance_wei = self.w3.eth.get_balance(Web3.to_checksum_address(META_MASK))
            return float(self.w3.from_wei(balance_wei, 'ether')) + self.get_binance_balance("ETH")
        except Exception:
            logger.error("Error fetching ETH balance", exc_info=True)
            return 0.0

    def _load_binance_balances(self) -> None:
        if self._binance_balances is not None or not self.binance_client:
            return
        try:
            account_info = self.binance_client.get_account()
            self._binance_balances = {
                entry["asset"]: float(entry["free"]) + float(entry["locked"])
                for entry in account_info.get("balances", [])
            }
        except Exception as err:
            logger.error("Binance account fetch error: %s", err)
            self._binance_balances = {}

    def refresh_binance_balances(self) -> None:
        if not self.binance_client:
            return
        self._binance_balances = None
        self._load_binance_balances()

    def get_binance_balance(self, symbol: str) -> float:
        if not self.binance_client:
            return 0.0
        self._load_binance_balances()
        return self._binance_balances.get(symbol.upper(), 0.0)

    def _fetch_hyperliquid_spot_balances(self) -> list[dict]:
        if not META_MASK:
            logger.warning("META_MASK not set; Hyperliquid balances will be 0.")
            return []
        try:
            url = "https://api.hyperliquid.xyz/info"
            payload = {"type": "spotClearinghouseState", "user": META_MASK}
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return response.json().get("balances", [])
        except Exception:
            logger.error("Error fetching balances from Hyperliquid", exc_info=True)
            return []

    def get_hyperliquid_balances(self) -> dict:
        return {entry["coin"]: float(entry.get("total", 0.0))
                for entry in self._fetch_hyperliquid_spot_balances()}

    def get_hyperliquid_free_balance(self, coin: str) -> float:
        """Free (sellable) balance = total - hold. `hold` is amount locked in open orders."""
        for entry in self._fetch_hyperliquid_spot_balances():
            if entry.get("coin") == coin:
                return float(entry.get("total", 0.0)) - float(entry.get("hold", 0.0))
        return 0.0

    def get_raw_kraken_balance(self) -> dict:
        if not self.kraken_client:
            return {}
        try:
            result = self.kraken_client.query_private("Balance")
            if result.get("error"):
                logger.error("Kraken API error: %s", result['error'])
                return {}
            return result["result"]
        except Exception as err:
            logger.error("Kraken balance fetch error: %s", err)
            return {}
