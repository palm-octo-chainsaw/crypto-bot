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
from data.prices import binance_lists_spot_pair

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

    ARBITRUM, BINANCE, KRAKEN = "arbitrum", "binance", "kraken"
    # Venues re-read on every get_venue_balances(). Binance is cached until an explicit
    # refresh, so its flag is owned by _load_binance_balances() instead.
    LIVE_VENUES = frozenset({ARBITRUM, KRAKEN})

    # Tracked symbols in report order; aggregate() appends any extra symbol a venue
    # was read for (see track_on_binance) after these.
    TRACKED_SYMBOLS = ("BTC", "PAXG", "SOL", "SUI", "USDC",
                       "ETH", "DOGE", "XRP", "LINK", "HYPE", "BNB", "NEAR")
    # Which symbols each venue is read for. A symbol left out of a venue's set is not
    # counted there even if the account holds it: PAXG is deliberately Kraken-only
    # because Binance rejects PAXG orders (-2010), so crediting a Binance PAXG balance
    # would let the planner size a leg that venue will never fill.
    BINANCE_SYMBOLS = frozenset({"BTC", "SOL", "SUI", "USDC", "ETH", "DOGE", "XRP", "LINK", "BNB", "HYPE",
                                 "NEAR"})
    ARBITRUM_SYMBOLS = frozenset({"USDC", "ETH"})

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
        self._degraded: set[str] = set()
        self._extra_binance: frozenset[str] = frozenset()
        self._binance_listed: dict[str, bool] = {}

    @property
    def degraded(self) -> set[str]:
        """Venues whose balances could not be read during the last spot-balance call.

        Every fetcher below falls back to 0.0 so one dead venue cannot stop the rest
        of the portfolio from being priced. That fallback is indistinguishable from
        an empty wallet, which is how a Binance + Kraken outage once persisted a
        $2.64 portfolio and turned a /performance baseline into +442,768%. Anything
        that stores or trades on these numbers has to consult this first.

        A missing client counts as degraded: the bot holds assets on all three venues,
        so absent credentials mean an incomplete read, not an empty wallet.
        """
        return set(self._degraded)

    def _mark_degraded(self, venue: str) -> None:
        self._degraded.add(venue)

    # web3 and krakenex both default to no request timeout, so a stalled socket
    # blocks the caller forever. Every call here runs on the bot's single event
    # loop: one hung read takes down command handling for the whole process.
    HTTP_TIMEOUT_SECONDS = 10

    @property
    def w3(self) -> Web3:
        if self._w3 is None or not self._w3.is_connected():
            self._w3 = Web3(Web3.HTTPProvider(
                self.ARBITRUM_RPC, request_kwargs={"timeout": self.HTTP_TIMEOUT_SECONDS}
            ))
            if not self._w3.is_connected():
                logger.warning("Unable to connect to Arbitrum RPC at %s", self.ARBITRUM_RPC)
                self._mark_degraded(self.ARBITRUM)
        return self._w3

    def _kraken_balance(self, symbol: str, kraken_raw: dict) -> float:
        kraken_key = self.KRAKEN_SYMBOL_MAP.get(symbol)
        if kraken_key is None:
            return 0.0
        return float(kraken_raw.get(kraken_key, 0.0))

    @property
    def effective_binance_symbols(self) -> frozenset[str]:
        return self.BINANCE_SYMBOLS | self._extra_binance

    def track_on_binance(self, symbols) -> None:
        """Also read Binance for these symbols, beyond TRACKED_SYMBOLS.

        A signal can name a token the static lists do not know. It is tracked only
        when Binance lists it against USDC: anything else could be neither priced nor
        traded, and its target is reported as untracked instead. The listing is
        cached for the life of the process.

        A failed listing check marks Binance degraded instead of dropping the symbol.
        A held position missing from the total would size every other leg against a
        smaller portfolio than the real one.
        """
        extra = set()
        for symbol in sorted(set(symbols) - set(self.TRACKED_SYMBOLS)):
            if symbol not in self._binance_listed:
                listed = binance_lists_spot_pair(symbol)
                if listed is None:
                    self._mark_degraded(self.BINANCE)
                    continue
                if not listed:
                    logger.warning("%s is not listed on Binance against USDC — not tracked", symbol)
                self._binance_listed[symbol] = listed
            if self._binance_listed[symbol]:
                extra.add(symbol)
        if extra - self._extra_binance:
            logger.info("Tracking on Binance beyond the static list: %s", ", ".join(sorted(extra)))
        self._extra_binance = frozenset(extra)

    def get_venue_balances(self) -> dict[str, dict[str, float]]:
        """Holdings split by venue: {venue: {symbol: amount}}.

        The bot has no transfer path between venues, so a rebalance leg can only be
        filled out of the balance sitting on the venue that will run it. Sizing off the
        aggregate instead sells a fraction of what was intended whenever the position is
        spread across venues — and silently reports that partial fill as a success.
        """
        # Clearing Binance here would drop a failure that just happened, since
        # update_portfolio() refreshes Binance before calling this.
        self._degraded -= self.LIVE_VENUES
        if not self.binance_client:
            self._mark_degraded(self.BINANCE)
        kraken_raw = self.get_raw_kraken_balance()

        return {
            self.BINANCE: {s: self.get_binance_balance(s) for s in self.effective_binance_symbols},
            self.KRAKEN: {s: self._kraken_balance(s, kraken_raw) for s in self.KRAKEN_SYMBOL_MAP},
            self.ARBITRUM: {"USDC": self._arbitrum_usdc(), "ETH": self._arbitrum_eth()},
        }

    @classmethod
    def aggregate(cls, venues: dict[str, dict[str, float]]) -> dict[str, float]:
        """Collapse a per-venue breakdown into one balance per tracked symbol."""
        extra = sorted({s for held in venues.values() for s in held} - set(cls.TRACKED_SYMBOLS))
        return {symbol: sum(held.get(symbol, 0.0) for held in venues.values())
                for symbol in (*cls.TRACKED_SYMBOLS, *extra)}

    def get_spot_balance(self) -> dict:
        return self.aggregate(self.get_venue_balances())

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
            self._mark_degraded(self.ARBITRUM)
            return 0.0

    def _arbitrum_usdc(self) -> float:
        return self._get_erc20_balance(self.USDC_CONTRACT_ADDRESS)

    def _arbitrum_eth(self) -> float:
        try:
            balance_wei = self.w3.eth.get_balance(Web3.to_checksum_address(META_MASK))
            return float(self.w3.from_wei(balance_wei, 'ether'))
        except Exception:
            logger.error("Error fetching ETH balance", exc_info=True)
            self._mark_degraded(self.ARBITRUM)
            return 0.0

    def get_usdc_balance(self) -> float:
        return self._arbitrum_usdc() + self.get_binance_balance("USDC")

    def get_eth_balance(self) -> float:
        return self._arbitrum_eth() + self.get_binance_balance("ETH")

    def _load_binance_balances(self) -> None:
        if self._binance_balances is not None or not self.binance_client:
            return
        try:
            account_info = self.binance_client.get_account()
            if "balances" not in account_info:
                # A 200 without the balances key is a malformed read, not an empty
                # account — treating it as empty is what zeroes the whole portfolio.
                raise ValueError("Binance account response has no 'balances' key")
            self._binance_balances = {
                entry["asset"]: float(entry["free"]) + float(entry["locked"])
                for entry in account_info["balances"]
            }
            self._degraded.discard(self.BINANCE)
        except Exception as err:
            logger.error("Binance account fetch error: %s", err)
            self._mark_degraded(self.BINANCE)
            self._binance_balances = {}

    def refresh_binance_balances(self) -> None:
        if not self.binance_client:
            self._mark_degraded(self.BINANCE)
            return
        self._binance_balances = None
        self._load_binance_balances()

    def get_binance_balance(self, symbol: str) -> float:
        if not self.binance_client:
            return 0.0
        self._load_binance_balances()
        return self._binance_balances.get(symbol.upper(), 0.0)

    def get_raw_kraken_balance(self) -> dict:
        if not self.kraken_client:
            self._mark_degraded(self.KRAKEN)
            return {}
        try:
            result = self.kraken_client.query_private("Balance", timeout=self.HTTP_TIMEOUT_SECONDS)
            if result.get("error"):
                logger.error("Kraken API error: %s", result['error'])
                self._mark_degraded(self.KRAKEN)
                return {}
            return result["result"]
        except Exception as err:
            logger.error("Kraken balance fetch error: %s", err)
            self._mark_degraded(self.KRAKEN)
            return {}
