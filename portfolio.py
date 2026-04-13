from typing import Dict, Tuple
from utils.helpers import load_json, setup_logging
from data.prices import fetch_prices
from data.trading import create_binance, execute_trade, find_direct_pair
from summary import Summary
from data.balance import Balance
from constants import BINANCE_API_KEY, BINANCE_API_SECRET, MIN_TRADE_USD, REBALANCE_RESERVE_PCT


logger = setup_logging('info')


def _is_directly_tradeable(exchange, token: str, stable: str) -> bool:
    """Check if a token has a direct trading pair with the stable asset (in either direction)."""
    return bool(find_direct_pair(exchange, token, stable)) or \
           bool(find_direct_pair(exchange, stable, token))


class Portfolio:
    def __init__(self):
        self.summary: Summary = Summary()
        self.balance: Balance = Balance()
        self.targets: dict = load_json("config/targets.json")
        self.portfolio: dict = self.balance.get_spot_balance()
        self.send_rebalance: bool = False

    def get_targets(self) -> Dict[str, int]:
        return self.targets

    def set_target(self, symbol: str, percent: int) -> Dict[str, int]:
        self.targets[symbol] = percent
        logger.debug("Target for %s set to %d%%", symbol, percent)
        return self.targets

    def update_portfolio(self) -> None:
        self.balance.refresh_binance_balances()
        self.portfolio = self.balance.get_spot_balance()
        logger.debug("Portfolio updated: %s", self.portfolio)

    def fetch_live_data(self) -> Tuple[dict, dict, float]:
        prices = fetch_prices(list(self.portfolio))
        values = {
            symbol: amount * prices[symbol]
            for symbol, amount in self.portfolio.items()
        }
        total_value = sum(values.values())
        logger.debug("Total: %s", total_value)
        return prices, values, total_value

    def evaluate_symbol(self, values: dict, total_value: float) -> None:
        for symbol, value in values.items():
            current_pct = (value / total_value) * 100
            target_pct = int(self.targets[symbol])
            diff = current_pct - target_pct

            msg = f"${symbol}: {current_pct:.2f}% (Target: {target_pct}%) {'🔺' if diff > 0 else '🔻'} {diff:.2f}%"
            logger.debug(msg)
            self.summary.add_summary(msg)

            if abs(diff) > 3:
                msg = f"⚠️ *Rebalance Needed*: ${symbol} is off by {diff:+.2f}% " \
                      f"(Current: {current_pct:.2f}%; Target: {target_pct}%)"
                self.summary.add_rebalance(msg)
                logger.debug("⚠️ %s is off by %.2f%% — consider rebalancing", symbol, diff)
                self.send_rebalance = True

    def _compute_rebalance(self, prices: dict, values: dict, total_value: float) -> Dict[str, float]:
        usable_value = total_value * (1 - REBALANCE_RESERVE_PCT / 100)
        return {
            symbol: ((self.targets[symbol] / 100) * usable_value - values[symbol])
                    / prices[symbol] if prices[symbol] > 0 else 0.0
            for symbol in self.portfolio
        }

    def calculate_rebalance(self, prices: dict, values: dict, total_value: float) -> Dict[str, float]:
        rebalance = self._compute_rebalance(prices, values, total_value)

        self.summary.add_rebalance("\n🧮 *Rebalance Plan*\n")
        for symbol, amount in rebalance.items():
            if abs(amount) < 1e-6:
                continue
            action = "Buy" if amount > 0 else "Sell"
            self.summary.add_rebalance(
                f"{action} [{abs(amount):.8f}] ${symbol} | "
                f"{'-' if action == 'Sell' else ''}${abs(amount * prices[symbol]):.2f} USD"
            )
        return rebalance

    def listener(self) -> str:
        self.send_rebalance = False
        self.update_portfolio()
        prices, values, total_value = self.fetch_live_data()
        self.evaluate_symbol(values, total_value)
        if self.send_rebalance:
            self.calculate_rebalance(prices, values, total_value)

        from data.database import record_snapshot, get_latest_signal_id
        record_snapshot(
            signal_id=get_latest_signal_id(),
            total_value_usd=total_value,
            balances=self.portfolio,
            prices=prices,
            values_usd=values,
            targets=self.targets,
        )

        return self.summary.flush_summary()

    def execute_rebalance(self, dry_run: bool = True) -> str:
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            return "⚠️ Binance API credentials not set — cannot execute trades."

        self.update_portfolio()
        prices, values, total_value = self.fetch_live_data()
        rebalance = self._compute_rebalance(prices, values, total_value)

        stable = "USDC"
        sells = {}
        buys = {}
        results = []
        for symbol, amount in rebalance.items():
            if symbol == stable:
                continue
            if symbol not in prices:
                logger.warning("No price data for %s — skipping", symbol)
                continue
            usd_value = abs(amount) * prices[symbol]
            if abs(amount) < 1e-6:
                continue
            if usd_value < MIN_TRADE_USD:
                logger.info("Trade for %s ($%.2f) below minimum $%.2f — skipping", symbol, usd_value, MIN_TRADE_USD)
                results.append({"symbol": symbol, "side": "sell" if amount < 0 else "buy", "amount": amount, "usd_value": usd_value, "dust": True})
                continue
            if amount < 0:
                sells[symbol] = abs(amount)
            else:
                buys[symbol] = amount

        if not sells and not buys:
            return "✅ Portfolio is balanced — no trades needed."

        try:
            exchange = create_binance(BINANCE_API_KEY, BINANCE_API_SECRET)
        except Exception as err:
            logger.error("Failed to connect to Binance: %s", err)
            return "⚠️ Failed to connect to Binance. Check logs for details."

        mode = "DRY RUN" if dry_run else "LIVE"

        for token, amount in sells.items():
            if not _is_directly_tradeable(exchange, token, stable):
                logger.info("Skipping %s — no Binance pair available", token)
                results.append({"symbol": f"{token}/{stable}", "side": "sell", "amount": amount, "skipped": True})
                continue
            try:
                trades = execute_trade(exchange, token, stable, amount, prices, stable, dry_run)
                results.extend(trades)
            except Exception as err:
                logger.error("Trade error selling %s: %s", token, err)
                results.append({"symbol": f"{token}/{stable}", "side": "sell", "amount": amount, "error": str(err)})

        for token, amount in buys.items():
            if not _is_directly_tradeable(exchange, token, stable):
                logger.info("Skipping %s — no Binance pair available", token)
                results.append({"symbol": f"{stable}/{token}", "side": "buy", "amount": amount, "skipped": True})
                continue
            try:
                stable_needed = amount * prices[token]
                trades = execute_trade(exchange, stable, token, stable_needed, prices, stable, dry_run)
                results.extend(trades)
            except Exception as err:
                logger.error("Trade error buying %s: %s", token, err)
                results.append({"symbol": f"{stable}/{token}", "side": "buy", "amount": amount, "error": str(err)})

        from data.database import record_trade, get_latest_signal_id
        signal_id = get_latest_signal_id()
        for trade in results:
            token = trade.get("symbol", "").split("/")[0]
            record_trade(
                signal_id=signal_id,
                symbol=trade.get("symbol", ""),
                side=trade.get("side", ""),
                amount=trade.get("amount", 0),
                price=prices.get(token),
                usd_value=trade.get("usd_value"),
                status="dust" if trade.get("dust") else
                       "skipped" if trade.get("skipped") else
                       "error" if trade.get("error") else
                       "dry_run" if trade.get("dry_run") else "filled",
                order_id=trade.get("id"),
                dry_run=dry_run,
                fee_amount=trade.get("fee_amount"),
                fee_currency=trade.get("fee_currency"),
                fee_rate=trade.get("fee_rate"),
            )

        lines = [f"🔄 *Rebalance {mode}*\n"]
        for trade in results:
            if trade.get("dust"):
                lines.append(f"🔸 DUST {trade['symbol']} (${trade['usd_value']:.2f}) — below ${MIN_TRADE_USD} minimum")
            elif trade.get("skipped"):
                lines.append(f"⏭️ SKIP {trade['symbol']} — not tradeable on Binance")
            elif "error" in trade:
                lines.append(f"❌ {trade['side'].upper()} {trade['symbol']}: trade failed (see logs)")
            elif trade.get("dry_run"):
                lines.append(f"📋 {trade['side'].upper()} `{trade['amount']}` {trade['symbol']}")
            else:
                lines.append(f"✅ {trade['side'].upper()} `{trade['amount']}` {trade['symbol']} — id: {trade.get('id', '?')}")

        return "\n".join(lines)
