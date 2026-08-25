"""Per-venue balance reads in data/balance: fallbacks, degradation and client setup."""
import pytest

import data.balance as balance_module
from data.balance import Balance

ADDRESS = "0x0000000000000000000000000000000000000001"


class FakeEth:
    def __init__(self, wei=0, raises=None):
        self._wei = wei
        self._raises = raises

    def get_balance(self, address):
        if self._raises is not None:
            raise self._raises
        return self._wei

    def contract(self, address=None, abi=None):
        return FakeContract()


class FakeWeb3Instance:
    def __init__(self, wei=0, raises=None, connected=True):
        self.eth = FakeEth(wei, raises)
        self._connected = connected

    def is_connected(self):
        return self._connected

    @staticmethod
    def from_wei(value, unit):
        return value / 10 ** 18


class FakeCall:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class FakeContract:
    def __init__(self, balance=2_500_000, decimals=6):
        self.functions = self
        self._balance = balance
        self._decimals = decimals

    def balanceOf(self, address):
        return FakeCall(self._balance)

    def decimals(self):
        return FakeCall(self._decimals)


class FakeKrakenClient:
    def __init__(self, result=None, raises=None):
        self._result = result or {"error": [], "result": {}}
        self._raises = raises
        self.calls = []

    def query_private(self, method, timeout=None):
        self.calls.append((method, timeout))
        if self._raises is not None:
            raise self._raises
        return self._result


def test_init_without_credentials_leaves_clients_unset(monkeypatch):
    monkeypatch.setattr(balance_module, "BINANCE_API_KEY", None)
    monkeypatch.setattr(balance_module, "BINANCE_API_SECRET", None)
    monkeypatch.setattr(balance_module, "KRAKEN_API_KEY", None)
    monkeypatch.setattr(balance_module, "KRAKEN_API_SECRET", None)

    b = Balance()

    assert b.binance_client is None
    assert b.kraken_client is None
    assert b.degraded == set()


def test_init_builds_clients_when_credentials_are_present(monkeypatch):
    monkeypatch.setattr(balance_module, "BINANCE_API_KEY", "k")
    monkeypatch.setattr(balance_module, "BINANCE_API_SECRET", "s")
    monkeypatch.setattr(balance_module, "KRAKEN_API_KEY", "k")
    monkeypatch.setattr(balance_module, "KRAKEN_API_SECRET", "s")
    monkeypatch.setattr(balance_module, "Client", lambda key, secret: ("binance", key))
    monkeypatch.setattr(balance_module.krakenex, "API", lambda key=None, secret=None: ("kraken", key))

    b = Balance()

    assert b.binance_client == ("binance", "k")
    assert b.kraken_client == ("kraken", "k")


def test_w3_marks_arbitrum_degraded_when_rpc_is_unreachable(monkeypatch, bare_balance):
    class UnreachableWeb3:
        def __init__(self, provider):
            pass

        @staticmethod
        def HTTPProvider(url, request_kwargs=None):
            return url

        def is_connected(self):
            return False

    monkeypatch.setattr(balance_module, "Web3", UnreachableWeb3)

    bare_balance.w3

    assert bare_balance.degraded == {Balance.ARBITRUM}


def test_w3_reuses_a_connected_instance(bare_balance):
    connected = FakeWeb3Instance(connected=True)
    bare_balance._w3 = connected

    assert bare_balance.w3 is connected
    assert bare_balance.degraded == set()


def test_kraken_balance_is_zero_for_untracked_symbols(bare_balance):
    assert bare_balance._kraken_balance("HYPE", {"HYPE": 10.0}) == 0.0
    assert bare_balance._kraken_balance("BTC", {"XXBT": 0.25}) == 0.25


def test_leverage_balance_reads_every_tracked_token(monkeypatch, bare_balance):
    seen = []
    monkeypatch.setattr(Balance, "_get_erc20_balance",
                        lambda self, address: seen.append(address) or 1.5)

    result = bare_balance.get_leverage_balance()

    assert set(result) == set(Balance.LEVERAGE_TOKENS)
    assert seen == list(Balance.LEVERAGE_TOKENS.values())
    assert all(value == 1.5 for value in result.values())


def test_erc20_balance_scales_by_decimals(monkeypatch, bare_balance):
    monkeypatch.setattr(balance_module, "META_MASK", ADDRESS)
    bare_balance._w3 = FakeWeb3Instance()

    assert bare_balance._get_erc20_balance(Balance.USDC_CONTRACT_ADDRESS) == 2.5
    assert bare_balance.degraded == set()


def test_erc20_balance_caches_the_contract(monkeypatch, bare_balance):
    monkeypatch.setattr(balance_module, "META_MASK", ADDRESS)
    bare_balance._w3 = FakeWeb3Instance()

    bare_balance._get_erc20_balance(Balance.USDC_CONTRACT_ADDRESS)
    bare_balance._get_erc20_balance(Balance.USDC_CONTRACT_ADDRESS)

    assert len(bare_balance._contracts) == 1


def test_erc20_balance_degrades_arbitrum_on_rpc_failure(monkeypatch, bare_balance):
    monkeypatch.setattr(balance_module, "META_MASK", ADDRESS)

    def boom(self, token_contract):
        raise RuntimeError("rpc timeout")
    monkeypatch.setattr(Balance, "_get_contract", boom)

    assert bare_balance._get_erc20_balance(Balance.USDC_CONTRACT_ADDRESS) == 0.0
    assert bare_balance.degraded == {Balance.ARBITRUM}


def test_eth_balance_converts_wei(monkeypatch, bare_balance):
    monkeypatch.setattr(balance_module, "META_MASK", ADDRESS)
    bare_balance._w3 = FakeWeb3Instance(wei=2 * 10 ** 18)

    assert bare_balance.get_eth_balance() == 2.0
    assert bare_balance.degraded == set()


def test_eth_balance_degrades_arbitrum_on_failure(monkeypatch, bare_balance):
    monkeypatch.setattr(balance_module, "META_MASK", ADDRESS)
    bare_balance._w3 = FakeWeb3Instance(raises=RuntimeError("rpc timeout"))

    assert bare_balance.get_eth_balance() == 0.0
    assert bare_balance.degraded == {Balance.ARBITRUM}


def test_usdc_balance_sums_arbitrum_and_binance(monkeypatch, bare_balance):
    monkeypatch.setattr(Balance, "_get_erc20_balance", lambda self, address: 100.0)
    monkeypatch.setattr(Balance, "get_binance_balance", lambda self, symbol: 25.0)

    assert bare_balance.get_usdc_balance() == 125.0


def test_refresh_binance_balances_degrades_without_a_client(bare_balance):
    bare_balance.refresh_binance_balances()

    assert bare_balance.degraded == {Balance.BINANCE}
    assert bare_balance._binance_balances is None


def test_refresh_binance_balances_rereads_the_account(bare_balance):
    class FakeBinanceClient:
        def __init__(self):
            self.calls = 0

        def get_account(self):
            self.calls += 1
            return {"balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1"}]}

    bare_balance.binance_client = FakeBinanceClient()
    bare_balance._binance_balances = {"BTC": 99.0}

    bare_balance.refresh_binance_balances()

    assert bare_balance.get_binance_balance("btc") == pytest.approx(0.6)
    assert bare_balance.binance_client.calls == 1


def test_hyperliquid_free_balance_skips_other_coins(monkeypatch, bare_balance):
    """The wallet holds several coins — the scan must walk past the ones we didn't ask for."""
    monkeypatch.setattr(
        Balance, "_fetch_hyperliquid_spot_balances",
        lambda self: [{"coin": "USDC", "total": "500.0", "hold": "0.0"},
                      {"coin": "PURR", "total": "12.0", "hold": "0.0"}],
    )

    assert bare_balance.get_hyperliquid_free_balance("HYPE") == 0.0


def test_raw_kraken_balance_degrades_without_a_client(bare_balance):
    assert bare_balance.get_raw_kraken_balance() == {}
    assert bare_balance.degraded == {Balance.KRAKEN}


def test_raw_kraken_balance_returns_the_result_payload(bare_balance):
    bare_balance.kraken_client = FakeKrakenClient(
        result={"error": [], "result": {"XXBT": "0.25"}})

    assert bare_balance.get_raw_kraken_balance() == {"XXBT": "0.25"}
    assert bare_balance.degraded == set()
    assert bare_balance.kraken_client.calls == [("Balance", Balance.HTTP_TIMEOUT_SECONDS)]


def test_raw_kraken_balance_degrades_on_api_error(bare_balance):
    bare_balance.kraken_client = FakeKrakenClient(
        result={"error": ["EGeneral:Invalid arguments"]})

    assert bare_balance.get_raw_kraken_balance() == {}
    assert bare_balance.degraded == {Balance.KRAKEN}


def test_raw_kraken_balance_degrades_on_transport_failure(bare_balance):
    bare_balance.kraken_client = FakeKrakenClient(raises=RuntimeError("connection reset"))

    assert bare_balance.get_raw_kraken_balance() == {}
    assert bare_balance.degraded == {Balance.KRAKEN}
