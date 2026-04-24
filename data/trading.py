"""Trade routing and execution via ccxt."""

import logging
import requests
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


def create_hyperliquid(wallet_address: str, private_key: str):
    exchange = ccxt.hyperliquid({
        "walletAddress": wallet_address,
        "privateKey": private_key,
        "enableRateLimit": True,
    })
    exchange.options["defaultSlippage"] = 0.005  # 0.5% max slippage for market orders
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


def _fetch_hyperliquid_fee(user_address: str, oid: str) -> dict:
    """Fetch fee info for a Hyperliquid order from the fills API."""
    try:
        payload = {
            "type": "userFillsByTime",
            "user": user_address,
            "startTime": 0,
            "aggregateByTime": False,
        }
        resp = requests.post("https://api.hyperliquid.xyz/info", json=payload, timeout=10)
        resp.raise_for_status()
        for fill in reversed(resp.json()):
            if str(fill.get("oid")) == str(oid):
                return {
                    "cost": float(fill.get("fee", 0)),
                    "currency": fill.get("feeToken"),
                }
    except Exception as err:
        logger.warning("Failed to fetch Hyperliquid fill fee: %s", err)
    return {}


def place_order(exchange, symbol: str, side: str, amount: float, dry_run: bool,
                price: float | None = None) -> dict:
    if dry_run:
        logger.info("[DRY RUN] %s %s on %s", side.upper(), amount, symbol)
        return {"symbol": symbol, "side": side, "amount": amount, "dry_run": True}
    logger.info("Executing: %s %s on %s", side.upper(), amount, symbol)
    order = exchange.create_market_order(symbol, side, amount, price=price)
    fee = order.get("fee") or {}
    # Hyperliquid doesn't return fees in the order response — fetch from fills
    if not fee and hasattr(exchange, 'walletAddress'):
        user = getattr(exchange, 'hyperliquid_user', exchange.walletAddress)
        fee = _fetch_hyperliquid_fee(user, order.get("id"))
    order["fee_amount"] = fee.get("cost")
    order["fee_currency"] = fee.get("currency")
    order["fee_rate"] = fee.get("rate")
    logger.info("Order filled: id=%s status=%s fee=%s %s",
                order["id"], order["status"], order["fee_amount"], order["fee_currency"])
    return order


def place_market_buy_cost(exchange, symbol: str, cost: float, dry_run: bool) -> dict:
    """Market-buy by spending an exact quote-currency amount (Binance quoteOrderQty)."""
    cost_precise = float(exchange.cost_to_precision(symbol, cost))
    if dry_run:
        logger.info("[DRY RUN] BUY cost=%s on %s", cost_precise, symbol)
        return {"symbol": symbol, "side": "buy", "cost": cost_precise, "dry_run": True}
    logger.info("Executing: BUY cost=%s on %s", cost_precise, symbol)
    order = exchange.create_market_buy_order_with_cost(symbol, cost_precise)
    fee = order.get("fee") or {}
    order["fee_amount"] = fee.get("cost")
    order["fee_currency"] = fee.get("currency")
    order["fee_rate"] = fee.get("rate")
    order["amount"] = order.get("filled") or order.get("amount") or 0
    order["cost"] = order.get("cost") or cost_precise
    logger.info("Order filled: id=%s status=%s filled=%s fee=%s %s",
                order.get("id"), order.get("status"), order["amount"],
                order["fee_amount"], order["fee_currency"])
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
