from utils.helpers import load_json, setup_logging
from data.prices import fetch_prices
from data.trading import create_binance, execute_trade, find_direct_pair
from data.database import record_snapshot, record_trade, get_latest_signal_id
from summary import Summary
from data.balance import Balance
from constants import BINANCE_API_KEY, BINANCE_API_SECRET, MIN_TRADE_USD, REBALANCE_RESERVE_PCT


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
    side = trade.get("side", "").upper()
    amount = trade.get("amount")

    if status == "dust":
        return f"🔸 DUST {symbol} (${trade['usd_value']:.2f}) — below ${MIN_TRADE_USD} minimum"
    if status == "skipped":
        return f"⏭️ SKIP {symbol} — not tradeable on Binance"
    if status == "error":
        return f"❌ {side} {symbol}: trade failed (see logs)"
    if status == "dry_run":
        return f"📋 {side} `{amount}` {symbol}"
    return f"✅ {side} `{amount}` {symbol} — id: {trade.get('id', '?')}"


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

    def _execute_side(self, exchange, tokens: dict, side: str, prices: dict, dry_run: bool) -> list:
        """Execute all trades on one side (sells or buys). Returns result dicts."""
        results = []
        for token, amount in tokens.items():
            pair_display = f"{token}/{STABLE}" if side == "sell" else f"{STABLE}/{token}"

            if not _is_directly_tradeable(exchange, token, STABLE):
                logger.info("Skipping %s — no Binance pair available", token)
                results.append({"symbol": pair_display, "side": side, "amount": amount, "skipped": True})
                continue

            try:
                if side == "sell":
                    results.extend(execute_trade(exchange, token, STABLE, amount, prices, STABLE, dry_run))
                else:
                    stable_needed = amount * prices[token]
                    results.extend(execute_trade(exchange, STABLE, token, stable_needed, prices, STABLE, dry_run))
            except Exception as err:
                logger.error("Trade error on %s %s: %s", side, token, err)
                results.append({"symbol": pair_display, "side": side, "amount": amount, "error": str(err)})
        return results

    def _persist_trades(self, results: list, prices: dict) -> None:
        signal_id = get_latest_signal_id()
        for trade in results:
            status = _trade_status(trade)
            if status not in ("filled", "error"):
                continue
            token = trade.get("symbol", "").split("/")[0]
            record_trade(
                signal_id=signal_id,
                symbol=trade.get("symbol", ""),
                side=trade.get("side", ""),
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

    def execute_rebalance(self, dry_run: bool = True) -> str:
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            return "⚠️ Binance API credentials not set — cannot execute trades."

        self.update_portfolio()
        prices, values, total_value = self.fetch_live_data()
        rebalance = self._compute_rebalance(prices, values, total_value)

        sells, buys, dust = self._plan_trades(rebalance, prices)
        if not sells and not buys:
            return "✅ Portfolio is balanced — no trades needed."

        try:
            exchange = create_binance(BINANCE_API_KEY, BINANCE_API_SECRET)
        except Exception as err:
            logger.error("Failed to connect to Binance: %s", err)
            return "⚠️ Failed to connect to Binance. Check logs for details."

        results = dust
        results.extend(self._execute_side(exchange, sells, "sell", prices, dry_run))
        results.extend(self._execute_side(exchange, buys, "buy", prices, dry_run))

        if not dry_run:
            self._persist_trades(results, prices)

        mode = "DRY RUN" if dry_run else "LIVE"
        lines = [f"🔄 *Rebalance {mode}*\n"] + [_format_trade_line(t) for t in results]
        return "\n".join(lines)
