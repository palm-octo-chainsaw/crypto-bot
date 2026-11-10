"""Placing a planned set of Hyperliquid legs against the account's USDC.

Spot buys, perp longs and perp shorts all draw on the same USDC, so the legs
that free USDC go first: every sell and cover is placed before any buy or open,
and the buys and opens are then scaled down together to the USDC left free.
"""

import logging
from dataclasses import dataclass, replace

from constants import MIN_TRADE_USD, SHORT_LEVERAGE
from data import hyperliquid_account
from data.trading import HYPERLIQUID_SLIPPAGE, apply_precision, effective_min_usd, place_hyperliquid_order

logger = logging.getLogger(__name__)

STABLE = "USDC"
# Hyperliquid lists a few low-priced coins per thousand units (SHIB as kSHIB).
# ccxt upper-cases the base, so the prefix is a capital K.
THOUSAND_PREFIX = "K"
# Perp longs are held unlevered: the margin is the position, as on spot.
LONG_LEVERAGE = 1


@dataclass(frozen=True)
class Leg:
    """One order to place. `contracts` are in the market's own units (kSHIB, not SHIB)."""
    symbol: str
    side: str
    contracts: float
    price: float
    perp: bool = False
    short: bool = False
    reduce_only: bool = False

    @property
    def usd(self) -> float:
        return self.contracts * self.price

    @property
    def leverage(self) -> int:
        return SHORT_LEVERAGE if self.short else LONG_LEVERAGE

    @property
    def margin_usd(self) -> float:
        """USDC the leg ties up when it opens, or frees when it closes."""
        return self.usd / self.leverage

    def result(self, amount: float | None = None, **flags) -> dict:
        """The leg as a trade result that never reached the venue."""
        return {"symbol": self.symbol, "side": self.side,
                "amount": self.contracts if amount is None else amount,
                "perp": self.perp, "short": self.short, "reduce_only": self.reduce_only, **flags}


def perp_symbol(exchange, coin: str) -> str | None:
    """Hyperliquid's USDC perp for `coin`, or None when it lists none."""
    for base in (coin, f"{THOUSAND_PREFIX}{coin}"):
        symbol = f"{base}/{STABLE}:{STABLE}"
        if symbol in exchange.markets:
            return symbol
    return None


def mid_price(exchange, symbol: str) -> float | None:
    """Mid price, from the market data load_markets() just fetched."""
    info = exchange.markets[symbol].get("info") or {}
    for key in ("midPx", "markPx"):
        try:
            price = float(info.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def account_state(exchange) -> tuple[str, dict, dict]:
    """(account mode, perp state, spot state) of the account the exchange trades for."""
    def info(request_type):
        return exchange.public_post_info({"type": request_type, "user": exchange.walletAddress})
    return info("userAbstraction"), info("clearinghouseState"), info("spotClearinghouseState")


def free_margin(exchange) -> float:
    return hyperliquid_account.free_margin(*account_state(exchange))


def _place(exchange, leg: Leg, dry_run: bool) -> dict:
    """Place one leg, setting cross leverage on a perp first when it opens one."""
    opens_perp = leg.perp and not leg.reduce_only
    if opens_perp and not dry_run:
        try:
            exchange.set_leverage(leg.leverage, leg.symbol, {"marginMode": "cross"})
        except Exception as err:
            logger.error("Could not set %sx cross leverage on %s: %s", leg.leverage, leg.symbol, err)
            return leg.result(error=f"leverage not set: {err}")
    try:
        amount = apply_precision(exchange, leg.symbol, leg.contracts)
    except Exception as err:
        logger.warning("Precision error for %s %s: %s", leg.side, leg.symbol, err)
        return leg.result(error=str(err))
    if amount <= 0:
        return leg.result(error="size below precision")
    try:
        order = place_hyperliquid_order(exchange, leg.symbol, leg.side, amount, leg.price,
                                        leg.reduce_only, dry_run)
    except Exception as err:
        logger.error("Hyperliquid %s %s failed: %s", leg.side, leg.symbol, err)
        return leg.result(amount, error=str(err))
    return {**order, "perp": leg.perp, "short": leg.short, "reduce_only": leg.reduce_only}


def execute_legs(exchange, exits: list[Leg], entries: list[Leg],
                 dry_run: bool) -> tuple[list, float]:
    """Place the exits, then the entries the free USDC can carry. Returns (results, unfunded USD).

    Entries are scaled down together when the USDC cannot carry them all, and the
    notional left out is returned, so the caller can say where that USDC sits.
    """
    results = [_place(exchange, leg, dry_run) for leg in exits]
    if not entries:
        return results, 0.0

    try:
        margin = free_margin(exchange)
    except Exception as err:
        logger.exception("Hyperliquid balance read failed before opening positions: %s", err)
        return results + [leg.result(error=f"Hyperliquid error: {err}") for leg in entries], 0.0
    if dry_run:
        # Nothing was closed, so count the USDC the exits would have freed.
        margin += sum(leg.margin_usd for leg in exits)

    wanted = sum(leg.margin_usd for leg in entries)
    # A market order can fill up to the slippage limit past the mid, and what it
    # costs or ties up is set at the fill, so keep that much headroom.
    scale = min(1.0, margin * (1 - HYPERLIQUID_SLIPPAGE) / wanted)
    if scale < 1.0:
        logger.warning("Free USDC covers %.1f%% of the buys and opens — scaling them down",
                       scale * 100)

    unfunded_usd = sum(leg.usd for leg in entries) * (1 - scale)
    for leg in entries:
        sized = replace(leg, contracts=leg.contracts * scale)
        floor = effective_min_usd(exchange, leg.symbol, MIN_TRADE_USD)
        if sized.usd < floor:
            if scale < 1.0:
                # Only the scaling pushed it under the minimum: it lacks USDC, not size.
                unfunded_usd += sized.usd
            else:
                results.append(sized.result(usd_value=sized.usd, min_usd=floor, dust=True))
            continue
        results.append(_place(exchange, sized, dry_run))
    return results, unfunded_usd
