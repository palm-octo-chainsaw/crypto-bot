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


def plan_longs(exchange, sells: dict[str, float], buys: dict[str, float], prices: dict,
               closing: set[str]) -> tuple[list, list, list]:
    """Turn the rebalance's coin amounts into (sells, buys, skipped) on Hyperliquid.

    Sells and buys are Legs; skipped are results. A sell is capped at what its
    market holds, and a coin in `closing` (its target is 0%) is sold in full, so
    no position is left behind too small to sell later. Buys are left unchecked
    against the minimum, since they are scaled to the free USDC before placing.
    """
    spot_held = _held_spot(exchange) if sells else {}
    perp_held = _held_perp_longs(exchange) if sells else {}
    exits, entries, skipped = [], [], []
    for side, planned in (("sell", sells), ("buy", buys)):
        for coin, amount in planned.items():
            routed = route(exchange, coin)
            if routed is None:
                logger.warning("No Hyperliquid market for %s — not traded", coin)
                skipped.append({"symbol": coin, "side": side, "amount": amount, "skipped": True})
                continue
            symbol, perp = routed
            price = mid_price(exchange, symbol)
            if price is None:
                skipped.append({"symbol": symbol, "side": side, "amount": amount,
                                "error": "no price on Hyperliquid"})
                continue
            # Sized in USD: a per-thousand perp's contracts are not the coin's units.
            leg = Leg(symbol, side, amount * prices[coin] / price, price, perp=perp,
                      reduce_only=perp and side == "sell")
            if side == "buy":
                entries.append(leg)
                continue
            held = perp_held.get(symbol, 0.0) if perp else max(spot_held.get(coin, 0.0), 0.0)
            if held <= 0:
                logger.warning("No %s held on Hyperliquid to sell", symbol)
                continue
            leg = Leg(symbol, side, held if coin in closing else min(leg.contracts, held), price,
                      perp=perp, reduce_only=perp)
            floor = effective_min_usd(exchange, symbol, MIN_TRADE_USD)
            # Hyperliquid waives its minimum for a reduce-only order that closes the
            # whole position, so a closing perp goes whatever its size; a spot
            # remnant that small cannot be sold.
            if leg.usd < floor and not (perp and coin in closing):
                skipped.append(leg.result(usd_value=leg.usd, min_usd=floor, dust=True))
                continue
            exits.append(leg)
    return exits, entries, skipped
