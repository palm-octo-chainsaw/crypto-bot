"""A signal's shorts, held as cross-margin perp shorts on Hyperliquid.

The long side never sees a short: its weight is part of the USDC target (see
utils.command_handlers._apply_allocations), and that USDC, sitting on
Hyperliquid, is these positions' margin. Each short is sized against the whole
portfolio, and only the difference from what Hyperliquid already holds is traded.
"""

import logging

from constants import MIN_TRADE_USD
from data.hyperliquid_book import STABLE, Leg, mid_price, perp_symbol
from data.trading import effective_min_usd

logger = logging.getLogger(__name__)

# A position worth less than this is closed outright rather than trimmed toward
# a target too small for any order Hyperliquid accepts.
CLOSED_USD = 0.01


def held_shorts(exchange) -> dict[str, float]:
    """Open short positions as {symbol: contracts}, contracts positive.

    A long is left out: it is either a long the bot holds as a perp, or one
    placed by hand, and neither is a short to cover.
    """
    held = {}
    for position in exchange.fetch_positions():
        contracts = float(position.get("contracts") or 0)
        if contracts and position.get("side") == "short":
            held[position["symbol"]] = contracts
    return held


def _short(symbol: str, side: str, contracts: float, price: float) -> Leg:
    return Leg(symbol, side, contracts, price, perp=True, short=True, reduce_only=side == "buy")


def plan_shorts(exchange, shorts: dict[str, float], book_usd: float) -> tuple[list, list, list]:
    """Split the change from held to target shorts into (covers, opens, skipped).

    `shorts` is {coin: % of the book}; `book_usd` is the book they are a share of.
    Covers and opens are Legs; skipped are results. A coin no longer shorted is
    covered in full; any other change smaller than the venue minimum is dust.
    """
    skipped = []
    target_usd = {}
    for coin, pct in shorts.items():
        symbol = perp_symbol(exchange, coin)
        if symbol is None:
            logger.warning("No Hyperliquid perp for %s — its short is held in %s", coin, STABLE)
            skipped.append(_short(coin, "sell", 0.0, 0.0).result(skipped=True))
            continue
        target_usd[symbol] = pct / 100 * book_usd

    held = held_shorts(exchange)
    covers, opens = [], []
    for symbol in sorted(target_usd.keys() | held.keys()):
        change = _change(exchange, symbol, target_usd.get(symbol, 0.0), held.get(symbol, 0.0))
        if isinstance(change, Leg):
            (opens if change.side == "sell" else covers).append(change)
        elif change:
            skipped.append(change)
    return covers, opens, skipped


def _change(exchange, symbol: str, target_usd: float, current: float) -> Leg | dict | None:
    """The leg taking one short from `current` contracts to `target_usd`.

    A result instead when it cannot be traded, None when there is nothing to do.
    """
    price = mid_price(exchange, symbol)
    if price is None:
        return _short(symbol, "sell", 0.0, 0.0).result(error="no price on Hyperliquid")
    if target_usd < CLOSED_USD and current:
        return _short(symbol, "buy", current, price)
    delta = target_usd / price - current
    leg = _short(symbol, "sell" if delta > 0 else "buy", abs(delta), price)
    floor = effective_min_usd(exchange, symbol, MIN_TRADE_USD)
    if leg.usd >= floor:
        return leg
    if leg.usd >= CLOSED_USD:
        return leg.result(usd_value=leg.usd, min_usd=floor, dust=True)
    return None
