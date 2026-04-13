"""Trade routing and execution via ccxt."""

import logging
import ccxt

logger = logging.getLogger(__name__)


def create_binance(api_key: str, api_secret: str):
    exchange = ccxt.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
    })
    exchange.load_markets()
    return exchange


def find_direct_pair(exchange, sell_token: str, buy_token: str):
    """Return (symbol, side) if a direct market exists, else None."""
    pair1 = f"{sell_token}/{buy_token}"
    if pair1 in exchange.markets:
        return pair1, "sell"
    pair2 = f"{buy_token}/{sell_token}"
    if pair2 in exchange.markets:
        return pair2, "buy"
    return None


def apply_precision(exchange, symbol: str, amount: float) -> float:
    return float(exchange.amount_to_precision(symbol, amount))


def place_order(exchange, symbol: str, side: str, amount: float, dry_run: bool) -> dict:
    if dry_run:
        logger.info("[DRY RUN] %s %s on %s", side.upper(), amount, symbol)
        return {"symbol": symbol, "side": side, "amount": amount, "dry_run": True}
    logger.info("Executing: %s %s on %s", side.upper(), amount, symbol)
    order = exchange.create_market_order(symbol, side, amount)
    fee = order.get("fee") or {}
    order["fee_amount"] = fee.get("cost")
    order["fee_currency"] = fee.get("currency")
    order["fee_rate"] = fee.get("rate")
    logger.info("Order filled: id=%s status=%s fee=%s %s",
                order["id"], order["status"], order["fee_amount"], order["fee_currency"])
    return order


def execute_trade(exchange, sell_token: str, buy_token: str, sell_amount: float,
                  prices: dict, stable: str, dry_run: bool) -> list:
    """Execute sell_token -> buy_token. Prefers direct pairs, falls back via stable."""
    trades = []
    direct = find_direct_pair(exchange, sell_token, buy_token)

    if direct:
        symbol, side = direct
        if side == "sell":
            amount = apply_precision(exchange, symbol, sell_amount)
            trades.append(place_order(exchange, symbol, "sell", amount, dry_run))
        else:
            buy_amount = (sell_amount * prices[sell_token]) / prices[buy_token]
            buy_amount = apply_precision(exchange, symbol, buy_amount)
            trades.append(place_order(exchange, symbol, "buy", buy_amount, dry_run))
    else:
        logger.info("No direct pair for %s->%s, routing via %s", sell_token, buy_token, stable)

        leg1 = find_direct_pair(exchange, sell_token, stable)
        if not leg1:
            logger.error("No pair found for %s/%s", sell_token, stable)
            return trades
        sym1, side1 = leg1
        if side1 == "sell":
            amt1 = apply_precision(exchange, sym1, sell_amount)
            trades.append(place_order(exchange, sym1, "sell", amt1, dry_run))
        else:
            amt1 = apply_precision(exchange, sym1, sell_amount * prices[sell_token])
            trades.append(place_order(exchange, sym1, "buy", amt1, dry_run))

        stable_received = sell_amount * prices[sell_token]
        leg2 = find_direct_pair(exchange, stable, buy_token)
        if not leg2:
            logger.error("No pair found for %s/%s", stable, buy_token)
            return trades
        sym2, side2 = leg2
        if side2 == "sell":
            amt2 = apply_precision(exchange, sym2, stable_received)
            trades.append(place_order(exchange, sym2, "sell", amt2, dry_run))
        else:
            amt2 = apply_precision(exchange, sym2, stable_received / prices[buy_token])
            trades.append(place_order(exchange, sym2, "buy", amt2, dry_run))

    return trades
