"""Reading a Hyperliquid account's USDC, which depends on the account's mode.

A unified account keeps one USDC balance for spot and perps: the spot balance is
the whole of it, perp margin and unrealized PnL included, and its `hold` is what
the perps have tied up. The perp state's account value is that same money seen
from the perp side, and its `withdrawable` reads 0, so adding the two counts the
perp equity twice. In the older modes spot and perps are separate balances, and
only the perp side can margin a perp. Portfolio margin borrows against spot
coins, which neither formula covers, so it is refused.

With the whole book on Hyperliquid, the account's spot tokens and long perps are
its coins as well (see holdings).
"""

UNIFIED = "unifiedAccount"
PORTFOLIO_MARGIN = "portfolioMargin"


class UnsupportedAccountMode(Exception):
    pass


def _spot_usdc(spot_state: dict) -> tuple[float, float]:
    """(total, hold) of the spot USDC balance."""
    for entry in spot_state.get("balances") or []:
        if entry.get("coin") == "USDC":
            return float(entry.get("total") or 0.0), float(entry.get("hold") or 0.0)
    return 0.0, 0.0


def _check_mode(mode: str) -> None:
    if mode == PORTFOLIO_MARGIN:
        raise UnsupportedAccountMode(
            "Hyperliquid account is in portfolio margin mode — switch it to Unified")


def usdc_equity(mode: str, perp_state: dict, spot_state: dict) -> float:
    """The account's USDC, perp margin and unrealized PnL included."""
    _check_mode(mode)
    total, _ = _spot_usdc(spot_state)
    if mode == UNIFIED:
        return total
    return float(perp_state["marginSummary"]["accountValue"]) + total


def free_margin(mode: str, perp_state: dict, spot_state: dict) -> float:
    """USDC free to margin a new perp position."""
    _check_mode(mode)
    if mode == UNIFIED:
        total, hold = _spot_usdc(spot_state)
        return max(total - hold, 0.0)
    return float(perp_state.get("withdrawable") or 0.0)


# Spot tokens the bot holds, by the coin they track. Bitcoin, Ether and Solana
# trade as Unit-bridged tokens; HYPE is native.
SPOT_TOKENS = {"UBTC": "BTC", "UETH": "ETH", "USOL": "SOL", "HYPE": "HYPE"}
# Perps quoted per thousand units are named with a lower-case k (kSHIB).
THOUSAND_PREFIX = "k"


def perp_coin(name: str) -> tuple[str, float]:
    """(coin, units per contract) for a perp's name."""
    if name.startswith(THOUSAND_PREFIX) and name[1:].isupper():
        return name[1:], 1_000.0
    return name, 1.0


def holdings(mode: str, perp_state: dict, spot_state: dict) -> dict[str, float]:
    """The account as {coin: amount}: spot tokens plus long perps, the rest in USDC.

    A long perp counts as the coin it tracks, and its notional comes out of the
    USDC, so the coins and the USDC add up to the account's equity. A short is
    left in the USDC: its weight is held there, and the shorts are sized from the
    signal, not from these holdings.
    """
    held: dict[str, float] = {}
    for entry in spot_state.get("balances") or []:
        coin = SPOT_TOKENS.get(entry.get("coin"))
        if coin:
            held[coin] = held.get(coin, 0.0) + float(entry.get("total") or 0.0)
    long_usd = 0.0
    for asset in perp_state.get("assetPositions") or []:
        position = asset.get("position") or {}
        size = float(position.get("szi") or 0.0)
        if size <= 0:
            continue
        coin, units = perp_coin(position["coin"])
        held[coin] = held.get(coin, 0.0) + size * units
        long_usd += float(position["positionValue"])
    held["USDC"] = usdc_equity(mode, perp_state, spot_state) - long_usd
    return held
