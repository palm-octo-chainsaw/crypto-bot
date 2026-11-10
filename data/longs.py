"""A signal's longs on Hyperliquid: on spot where its book is deep, as a 1x perp elsewhere.

Only Bitcoin, Ether, Solana and HYPE have spot books on Hyperliquid about as
deep as their perps, so only they are bought outright and pay no funding. Every
other coin has no spot market there, or one too thin to trade, and is held as an
unlevered cross-margin perp long, which follows the price the same way but pays
(or earns) funding every hour.
"""

import logging

from constants import MIN_TRADE_USD
from data.hyperliquid_account import SPOT_TOKENS
from data.hyperliquid_book import STABLE, Leg, mid_price, perp_symbol
from data.trading import effective_min_usd

logger = logging.getLogger(__name__)

SPOT_COINS = frozenset(SPOT_TOKENS.values())


def route(exchange, coin: str) -> tuple[str, bool] | None:
    """(symbol, is a perp) a long in `coin` is held on, or None when Hyperliquid lists neither."""
    spot = f"{coin}/{STABLE}"
    if coin in SPOT_COINS and spot in exchange.markets:
        return spot, False
    perp = perp_symbol(exchange, coin)
    return (perp, True) if perp else None


def _held_spot(exchange) -> dict[str, float]:
    """Spot coins free to sell, as {coin: amount}."""
    state = exchange.public_post_info({"type": "spotClearinghouseState", "user": exchange.walletAddress})
    held = {}
    for entry in state.get("balances") or []:
        coin = SPOT_TOKENS.get(entry.get("coin"))
        if coin:
            held[coin] = float(entry.get("total") or 0.0) - float(entry.get("hold") or 0.0)
    return held


def _held_perp_longs(exchange) -> dict[str, float]:
    """Open perp longs as {symbol: contracts}."""
    return {p["symbol"]: float(p["contracts"]) for p in exchange.fetch_positions()
            if p.get("side") == "long" and float(p.get("contracts") or 0)}


def _leg(exchange, coin: str, side: str, amount: float, prices: dict) -> Leg | dict:
    """`amount` of `coin` as a Leg on its route, or a result when it cannot be traded."""
    routed = route(exchange, coin)
    if routed is None:
        logger.warning("No Hyperliquid market for %s — not traded", coin)
        return {"symbol": coin, "side": side, "amount": amount, "skipped": True}
    symbol, perp = routed
    price = mid_price(exchange, symbol)
    if price is None:
        return {"symbol": symbol, "side": side, "amount": amount, "error": "no price on Hyperliquid"}
    # Sized in USD: a per-thousand perp's contracts are not the coin's units.
    return Leg(symbol, side, amount * prices[coin] / price, price, perp=perp,
               reduce_only=perp and side == "sell")


def _capped_sell(exchange, leg: Leg, held: float, closing: bool) -> Leg | dict | None:
    """The sell capped at what its market holds, all of it when `closing`.

    A result instead when it is dust, None when nothing is held there.
    """
    if held <= 0:
        logger.warning("No %s held on Hyperliquid to sell", leg.symbol)
        return None
    # The whole position exactly: a float off by a hair would leave a remnant open.
    leg = leg.sized(held if closing else min(leg.contracts, held))
    floor = effective_min_usd(exchange, leg.symbol, MIN_TRADE_USD)
    # Hyperliquid waives its minimum for a reduce-only order that closes the
    # whole position, so a closing perp goes whatever its size; a spot
    # remnant that small cannot be sold.
    if leg.usd < floor and not (leg.perp and closing):
        return leg.result(usd_value=leg.usd, min_usd=floor, dust=True)
    return leg


def plan_longs(exchange, sells: dict[str, float], buys: dict[str, float], prices: dict,
               closing: set[str]) -> tuple[list, list, list]:
    """Turn the rebalance's coin amounts into (sells, buys, skipped) on Hyperliquid.

    Sells and buys are Legs; skipped are results. A sell is capped at what its
    market holds, and a coin in `closing` (its target is 0%) is sold in full, so
    no position is left behind too small to sell later. Buys are left unchecked
    against the minimum, since they are scaled to the free USDC before placing.
    """
    exits, entries, skipped = [], [], []
    for coin, amount in buys.items():
        leg = _leg(exchange, coin, "buy", amount, prices)
        (entries if isinstance(leg, Leg) else skipped).append(leg)

    spot_held = _held_spot(exchange) if sells else {}
    perp_held = _held_perp_longs(exchange) if sells else {}
    for coin, amount in sells.items():
        leg = _leg(exchange, coin, "sell", amount, prices)
        if isinstance(leg, Leg):
            held = perp_held.get(leg.symbol, 0.0) if leg.perp else max(spot_held.get(coin, 0.0), 0.0)
            leg = _capped_sell(exchange, leg, held, coin in closing)
        if isinstance(leg, Leg):
            exits.append(leg)
        elif leg:
            skipped.append(leg)
    return exits, entries, skipped
