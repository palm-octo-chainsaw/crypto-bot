"""Trade routing and execution via ccxt."""

import logging
import ccxt

logger = logging.getLogger(__name__)


# With credentials, ccxt also loads Binance's tokenized US stocks, keyed like spot
# pairs. A ticker clash then replaces the crypto market: SUI/USDC became Sun
# Communities stock, so every SUI order went to /sapi/v1/equity/order/place and
# was rejected with 486213 "Stock trading account not found". The bot only
# trades crypto spot here, so nothing else is loaded.
BINANCE_MARKET_TYPES = ["spot"]


def create_binance(api_key: str, api_secret: str):
    exchange = ccxt.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"fetchMarkets": {"types": BINANCE_MARKET_TYPES}},
    })
    exchange.load_markets()
    return exchange


# Worst price a Hyperliquid market order may fill at, as a fraction of the mid.
# Hyperliquid has no true market order: ccxt sends an IOC limit this far through
# the book, and anything the book cannot fill inside it is cancelled.
HYPERLIQUID_SLIPPAGE = 0.01


def create_hyperliquid(account_address: str, agent_key: str):
    """Hyperliquid spot and perps, signed by an API wallet trading for the main account.

    An API (agent) wallet can place and cancel orders but cannot withdraw, so the
    key the bot holds cannot move funds off the account.
    """
    exchange = ccxt.hyperliquid({
        "walletAddress": account_address,
        "privateKey": agent_key,
        "enableRateLimit": True,
        "options": {"defaultType": "swap", "defaultSlippage": HYPERLIQUID_SLIPPAGE},
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


def min_notional(exchange, symbol: str) -> float:
    """Exchange-enforced minimum order value in quote currency, 0.0 if unknown.

    Binance rejects orders under its per-symbol NOTIONAL filter with -1013, which a
    flat local floor can't predict — ETH/USDC sits near $5 while other pairs differ.
    """
    market = (exchange.markets or {}).get(symbol) or {}
    cost_limits = (market.get("limits") or {}).get("cost") or {}
    try:
        return float(cost_limits.get("min") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def effective_min_usd(exchange, symbol: str, floor: float) -> float:
    """The larger of our own minimum and the exchange's, so a leg can't pass locally
    and then be rejected on submission."""
    return max(floor, min_notional(exchange, symbol))


def _restore_order_identity(order: dict, symbol: str, side: str) -> dict:
    """Fill in what the venue left out of its order response.

    Some venues answer a market order with the fill alone — no coin or side — so
    ccxt parses `symbol` and `side` as None. That once reached Telegram as
    "✅ `23.33` None" and, worse, `_persist_trades` wrote the row with an empty
    symbol and no price, leaving those fills unattributable in the trade history.
    We asked for this order, so the request is the authority on what it was; only
    fields the venue actually returned win.
    """
    order["symbol"] = order.get("symbol") or symbol
    order["side"] = order.get("side") or side
    return order


def place_order(exchange, symbol: str, side: str, amount: float, dry_run: bool) -> dict:
    if dry_run:
        logger.info("[DRY RUN] %s %s on %s", side.upper(), amount, symbol)
        return {"symbol": symbol, "side": side, "amount": amount, "dry_run": True}
    logger.info("Executing: %s %s on %s", side.upper(), amount, symbol)
    order = _restore_order_identity(
        exchange.create_market_order(symbol, side, amount), symbol, side
    )
    order["amount"] = order.get("amount") or amount
    fee = order.get("fee") or {}
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
    order = _restore_order_identity(
        exchange.create_market_buy_order_with_cost(symbol, cost_precise), symbol, "buy"
    )
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


def _hyperliquid_fill_fee(exchange, symbol: str, order_id: str | None, since: int) -> dict:
    """Fee paid on a Hyperliquid order, summed over its fills.

    The order response carries the fill but not the fee, which only the user's
    fills report. Best effort: a failed lookup leaves the trade without a fee.
    """
    if order_id is None:
        return {}
    try:
        fills = [t for t in exchange.fetch_my_trades(symbol, since=since)
                 if str(t.get("order")) == str(order_id)]
    except Exception as err:
        logger.warning("Failed to fetch Hyperliquid fill fee for %s: %s", order_id, err)
        return {}
    fees = [t["fee"] for t in fills if t.get("fee") and t["fee"].get("cost") is not None]
    if not fees:
        return {}
    return {"cost": sum(float(f["cost"]) for f in fees), "currency": fees[0].get("currency")}


def place_hyperliquid_order(exchange, symbol: str, side: str, amount: float, price: float,
                            reduce_only: bool, dry_run: bool) -> dict:
    """Market order on a Hyperliquid spot pair or perp.

    `price` is the reference the slippage limit is set from. Only a perp can be
    reduce-only, so a spot order carries no such flag.
    """
    if dry_run:
        logger.info("[DRY RUN] %s %s on %s%s", side.upper(), amount, symbol,
                    " (reduce-only)" if reduce_only else "")
        return {"symbol": symbol, "side": side, "amount": amount, "dry_run": True}
    logger.info("Executing: %s %s on %s%s", side.upper(), amount, symbol,
                " (reduce-only)" if reduce_only else "")
    since = exchange.milliseconds()
    order = _restore_order_identity(
        exchange.create_order(symbol, "market", side, amount, price,
                              {"reduceOnly": True} if reduce_only else {}),
        symbol, side,
    )
    order["amount"] = order.get("filled") or order.get("amount") or amount
    average = order.get("average") or order.get("price")
    order["cost"] = order.get("cost") or (order["amount"] * average if average else None)
    fee = order.get("fee") or _hyperliquid_fill_fee(exchange, symbol, order.get("id"), since)
    order["fee_amount"] = fee.get("cost")
    order["fee_currency"] = fee.get("currency")
    order["fee_rate"] = fee.get("rate")
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
