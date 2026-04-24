from utils.helpers import load_json, setup_logging
from data.prices import fetch_prices
from data.trading import create_binance, create_hyperliquid, find_direct_pair, place_order, place_market_buy_cost, apply_precision
from data.database import record_snapshot, record_trade, get_latest_signal_id
from summary import Summary
from data.balance import Balance
from constants import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    HYPERLIQUID_PRIVATE_KEY, HYPERLIQUID_ACCOUNT_ADDRESS, META_MASK,
    MIN_TRADE_USD, REBALANCE_RESERVE_PCT,
)


logger = setup_logging('info')

STABLE = "USDC"
REBALANCE_THRESHOLD_PCT = 3.0


def _is_directly_tradeable(exchange, token: str, stable: str) -> bool:
    return bool(find_direct_pair(exchange, token, stable)) or \
           bool(find_direct_pair(exchange, stable, token))


def _trade_status(trade: dict) -> str:
    if trade.get("dust"):
        return "dust"
    if trade.get("skipped"):
        return "skipped"
    if trade.get("error"):
        return "error"
    if trade.get("dry_run"):
        return "dry_run"
    return "filled"


def _format_trade_line(trade: dict) -> str:
    status = _trade_status(trade)
    symbol = trade["symbol"]
    side = (trade.get("side") or "").upper()
    amount = trade.get("amount")
    cost = trade.get("cost")
    qty = f"${cost:.2f} {STABLE}" if amount in (None, 0) and cost else f"`{amount}`"

    if status == "dust":
        return f"🔸 DUST {symbol} (${trade['usd_value']:.2f}) — below ${MIN_TRADE_USD} minimum"
    if status == "skipped":
        return f"⏭️ SKIP {symbol} — no exchange pair available"
    if status == "error":
        return f"❌ {side} {symbol}: trade failed (see logs)"
    if status == "dry_run":
        return f"📋 {side} {qty} {symbol}"
    return f"✅ {side} {qty} {symbol} — id: {trade.get('id', '?')}"


class Portfolio:
    def __init__(self):
        self.summary: Summary = Summary()
        self.balance: Balance = Balance()
        self.targets: dict = load_json("config/targets.json")
        self.portfolio: dict = self.balance.get_spot_balance()
        self.send_rebalance: bool = False

    def get_targets(self) -> dict:
        return self.targets

    def set_target(self, symbol: str, percent: int) -> dict:
        self.targets[symbol] = percent
        logger.debug("Target for %s set to %d%%", symbol, percent)
        return self.targets

    def update_portfolio(self) -> None:
        self.balance.refresh_binance_balances()
        self.portfolio = self.balance.get_spot_balance()
        logger.debug("Portfolio updated: %s", self.portfolio)

    def fetch_live_data(self) -> tuple[dict, dict, float]:
        prices = fetch_prices(list(self.portfolio))
        values = {symbol: amount * prices[symbol] for symbol, amount in self.portfolio.items()}
        total_value = sum(values.values())
        logger.debug("Total: %s", total_value)
        return prices, values, total_value

    def evaluate_symbol(self, values: dict, total_value: float) -> None:
        for symbol, value in values.items():
            current_pct = (value / total_value) * 100
            target_pct = int(self.targets[symbol])
            diff = current_pct - target_pct
            arrow = "🔺" if diff > 0 else "🔻"

            self.summary.add_summary(
                f"${symbol}: {current_pct:.2f}% (Target: {target_pct}%) {arrow} {diff:.2f}%"
            )

            if abs(diff) > REBALANCE_THRESHOLD_PCT:
                self.summary.add_rebalance(
                    f"⚠️ *Rebalance Needed*: ${symbol} is off by {diff:+.2f}% "
                    f"(Current: {current_pct:.2f}%; Target: {target_pct}%)"
                )
                self.send_rebalance = True

    def _compute_rebalance(self, prices: dict, values: dict, total_value: float) -> dict[str, float]:
        usable_value = total_value * (1 - REBALANCE_RESERVE_PCT / 100)
        return {
            symbol: ((self.targets[symbol] / 100) * usable_value - values[symbol]) / prices[symbol]
                    if prices[symbol] > 0 else 0.0
            for symbol in self.portfolio
        }

    def calculate_rebalance(self, prices: dict, values: dict, total_value: float) -> dict[str, float]:
        rebalance = self._compute_rebalance(prices, values, total_value)

        self.summary.add_rebalance("\n🧮 *Rebalance Plan*\n")
        for symbol, amount in rebalance.items():
            if abs(amount) < 1e-6:
                continue
            action = "Buy" if amount > 0 else "Sell"
            sign = "-" if action == "Sell" else ""
            self.summary.add_rebalance(
                f"{action} [{abs(amount):.8f}] ${symbol} | {sign}${abs(amount * prices[symbol]):.2f} USD"
            )
        return rebalance

    def listener(self) -> str:
        self.send_rebalance = False
        self.update_portfolio()
        prices, values, total_value = self.fetch_live_data()
        self.evaluate_symbol(values, total_value)
        if self.send_rebalance:
            self.calculate_rebalance(prices, values, total_value)

        record_snapshot(
            signal_id=get_latest_signal_id(),
            total_value_usd=total_value,
            balances=self.portfolio,
            prices=prices,
            values_usd=values,
            targets=self.targets,
        )
        return self.summary.flush_summary()

    def _plan_trades(self, rebalance: dict, prices: dict) -> tuple[dict, dict, list]:
        """Classify rebalance amounts into sells, buys, and dust trades."""
        sells, buys, dust = {}, {}, []
        for symbol, amount in rebalance.items():
            if symbol == STABLE or abs(amount) < 1e-6:
                continue
            if symbol not in prices:
                logger.warning("No price data for %s — skipping", symbol)
                continue

            usd_value = abs(amount) * prices[symbol]
            side = "sell" if amount < 0 else "buy"

            if usd_value < MIN_TRADE_USD:
                logger.info("Trade for %s ($%.2f) below minimum $%.2f — skipping", symbol, usd_value, MIN_TRADE_USD)
                dust.append({"symbol": symbol, "side": side, "amount": amount, "usd_value": usd_value, "dust": True})
            elif amount < 0:
                sells[symbol] = abs(amount)
            else:
                buys[symbol] = amount
        return sells, buys, dust

    def _execute_sells(self, exchange, sells: dict, dry_run: bool) -> list:
        """Sell planned_amount/holdings (capped at 1.0) of each token's free exchange balance."""
        results = []
        if not sells:
            return results
        free = exchange.fetch_balance().get("free", {})

        for token, planned_amount in sells.items():
            pair_display = f"{token}/{STABLE}"
            if not _is_directly_tradeable(exchange, token, STABLE):
                logger.info("Skipping %s — no Binance pair available", token)
                results.append({"symbol": pair_display, "side": "sell", "amount": planned_amount, "skipped": True})
                continue

            holdings = self.portfolio.get(token, 0.0)
            free_amount = float(free.get(token, 0.0))
            if holdings <= 0 or free_amount <= 0:
                logger.warning("No %s balance to sell (holdings=%s, free=%s)", token, holdings, free_amount)
                results.append({"symbol": pair_display, "side": "sell", "amount": planned_amount,
                                "error": "zero balance"})
                continue

            fraction = min(planned_amount / holdings, 1.0)
            sell_amount = apply_precision(exchange, f"{token}/{STABLE}", free_amount * fraction)
            if sell_amount <= 0:
                logger.warning("Sell size for %s rounded to 0 (free=%s fraction=%.4f)", token, free_amount, fraction)
                results.append({"symbol": pair_display, "side": "sell", "amount": 0, "error": "size below precision"})
                continue

            try:
                results.append(place_order(exchange, f"{token}/{STABLE}", "sell", sell_amount, dry_run))
            except Exception as err:
                logger.error("Trade error on sell %s: %s", token, err)
                results.append({"symbol": pair_display, "side": "sell", "amount": sell_amount, "error": str(err)})
        return results

    def _execute_buys(self, exchange, buys: dict, prices: dict, dry_run: bool) -> list:
        """Spend per-token fraction of fresh free USDC (quoteOrderQty) weighted by intended USD."""
        results = []
        if not buys:
            return results
        free = exchange.fetch_balance().get("free", {})
        free_stable = float(free.get(STABLE, 0.0))

        total_intended_usd = sum(amt * prices[tok] for tok, amt in buys.items())
        if total_intended_usd <= 0 or free_stable <= 0:
            for token, amount in buys.items():
                results.append({"symbol": f"{STABLE}/{token}", "side": "buy", "amount": amount,
                                "error": f"no {STABLE} to spend"})
            return results

        for token, planned_amount in buys.items():
            pair_display = f"{STABLE}/{token}"
            if not _is_directly_tradeable(exchange, token, STABLE):
                logger.info("Skipping %s — no Binance pair available", token)
                results.append({"symbol": pair_display, "side": "buy", "amount": planned_amount, "skipped": True})
                continue

            intended_usd = planned_amount * prices[token]
            fraction = intended_usd / total_intended_usd
            cost = free_stable * fraction
            symbol = f"{token}/{STABLE}"
            try:
                results.append(place_market_buy_cost(exchange, symbol, cost, dry_run))
            except Exception as err:
                logger.error("Trade error on buy %s: %s", token, err)
                results.append({"symbol": pair_display, "side": "buy", "amount": planned_amount,
                                "cost": cost, "error": str(err)})
        return results

    def _persist_trades(self, results: list, prices: dict) -> None:
        signal_id = get_latest_signal_id()
        for trade in results:
            status = _trade_status(trade)
            if status not in ("filled", "error"):
                continue
            token = (trade.get("symbol") or "").split("/")[0]
            record_trade(
                signal_id=signal_id,
                symbol=trade.get("symbol") or "",
                side=trade.get("side") or "",
                amount=trade.get("amount", 0),
                price=prices.get(token),
                usd_value=trade.get("usd_value"),
                status=status,
                order_id=trade.get("id"),
                dry_run=False,
                fee_amount=trade.get("fee_amount"),
                fee_currency=trade.get("fee_currency"),
                fee_rate=trade.get("fee_rate"),
            )

    HYPE_PAIR = "HYPE/USDC"

    def _execute_hype(self, amount: float, side: str, prices: dict, dry_run: bool) -> list:
        """Execute HYPE trade on Hyperliquid. Returns result dicts."""
        if not HYPERLIQUID_PRIVATE_KEY or not HYPERLIQUID_ACCOUNT_ADDRESS:
            logger.warning("Hyperliquid credentials not set — skipping HYPE trade")
            return [{"symbol": self.HYPE_PAIR, "side": side, "amount": abs(amount), "skipped": True}]

        try:
            hl = create_hyperliquid(HYPERLIQUID_ACCOUNT_ADDRESS, HYPERLIQUID_PRIVATE_KEY)
            hl.hyperliquid_user = META_MASK
        except Exception as err:
            logger.error("Failed to connect to Hyperliquid: %s", err)
            return [{"symbol": self.HYPE_PAIR, "side": side, "amount": abs(amount), "error": str(err)}]

        symbol = self.HYPE_PAIR
        try:
            trade_amount = abs(amount)
            if side == "sell":
                free_hype = float(hl.fetch_balance().get("free", {}).get("HYPE", 0.0))
                trade_amount = min(trade_amount, free_hype)
            trade_amount = apply_precision(hl, symbol, trade_amount)
            hype_price = prices.get("HYPE")
            result = place_order(hl, symbol, side, trade_amount, dry_run, price=hype_price)
            return [result]
        except Exception as err:
            logger.error("Hyperliquid HYPE trade error: %s", err)
            return [{"symbol": symbol, "side": side, "amount": abs(amount), "error": str(err)}]

    def execute_rebalance(self, dry_run: bool = True) -> str:
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            return "⚠️ Binance API credentials not set — cannot execute trades."

        self.update_portfolio()
        prices, values, total_value = self.fetch_live_data()
        rebalance = self._compute_rebalance(prices, values, total_value)

        sells, buys, dust = self._plan_trades(rebalance, prices)
        if not sells and not buys:
            return "✅ Portfolio is balanced — no trades needed."

        results = list(dust)

        # Execute HYPE on Hyperliquid first
        if "HYPE" in sells:
            results.extend(self._execute_hype(sells.pop("HYPE"), "sell", prices, dry_run))
        if "HYPE" in buys:
            results.extend(self._execute_hype(buys.pop("HYPE"), "buy", prices, dry_run))

        # Execute remaining trades on Binance
        if sells or buys:
            try:
                exchange = create_binance(BINANCE_API_KEY, BINANCE_API_SECRET)
            except Exception as err:
                logger.error("Failed to connect to Binance: %s", err)
                return "⚠️ Failed to connect to Binance. Check logs for details."

            results.extend(self._execute_sells(exchange, sells, dry_run))
            results.extend(self._execute_buys(exchange, buys, prices, dry_run))

        if not dry_run:
            self._persist_trades(results, prices)

        mode = "DRY RUN" if dry_run else "LIVE"
        lines = [f"🔄 *Rebalance {mode}*\n"] + [_format_trade_line(t) for t in results]
        return "\n".join(lines)
