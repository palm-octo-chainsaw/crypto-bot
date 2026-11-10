from utils.helpers import load_json, setup_logging, split_shorts
from data.prices import fetch_prices
from data.trading import (
    create_binance, create_hyperliquid, find_direct_pair, place_order,
    place_market_buy_cost, apply_precision, effective_min_usd,
)
from data.database import record_snapshot, record_trade, get_latest_signal_id, get_latest_allocations
from data.hyperliquid_account import UNIFIED, UnsupportedAccountMode, perp_coin
from data.hyperliquid_book import account_state, execute_legs, mid_price, perp_symbol
from data.longs import plan_longs
from data.shorts import held_shorts, plan_shorts
from summary import Summary
from data.balance import Balance
import constants
from constants import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    HYPERLIQUID_ACCOUNT_ADDRESS, HYPERLIQUID_PRIVATE_KEY,
    MIN_TRADE_USD, REBALANCE_RESERVE_PCT, MANUAL_ASSETS,
)


logger = setup_logging('info')

STABLE = "USDC"
REBALANCE_THRESHOLD_PCT = 3.0
# Summary lines for tokens both held and targeted below this are dust and left out.
SUMMARY_MIN_PCT = 0.05
ERR_SIZE_BELOW_PRECISION = "size below precision"
# Below a cent, a cross-matched leg counts as fully executed rather than leftover.
MATCHED_USD = 0.01


def _is_directly_tradeable(exchange, token: str, stable: str) -> bool:
    return bool(find_direct_pair(exchange, token, stable)) or \
           bool(find_direct_pair(exchange, stable, token))


def _trade_status(trade: dict) -> str:
    if trade.get("manual"):
        return "manual"
    if trade.get("dust"):
        return "dust"
    if trade.get("skipped"):
        return "skipped"
    if trade.get("unavailable"):
        return "unavailable"
    if trade.get("error"):
        return "error"
    if trade.get("dry_run"):
        return "dry_run"
    return "filled"


def _side_label(trade: dict) -> str:
    """BUY/SELL for a long; a short's leg says what it does to the short."""
    if trade.get("short"):
        return "COVER" if trade.get("side") == "buy" else "SHORT"
    return (trade.get("side") or "").upper()


def _format_trade_line(trade: dict) -> str:
    status = _trade_status(trade)
    symbol = trade["symbol"]
    side = _side_label(trade)
    amount = trade.get("amount")
    cost = trade.get("cost")
    qty = f"${cost:.2f} {STABLE}" if amount in (None, 0) and cost else f"`{amount}`"

    if status == "manual":
        return (f"✋ MANUAL {side} {symbol} (${trade['usd_value']:.2f}) — "
                f"no bot venue trades {symbol}, execute it manually")
    if status == "dust":
        return f"🔸 DUST {symbol} (${trade['usd_value']:.2f}) — below ${trade.get('min_usd', MIN_TRADE_USD):.2f} minimum"
    if status == "skipped":
        if trade.get("short"):
            return f"⏭️ SKIP {symbol} short — no Hyperliquid perp, held in {STABLE}"
        return f"⏭️ SKIP {symbol} — no exchange pair available"
    if status == "unavailable" and trade.get("venue") == Balance.HYPERLIQUID:
        where = trade.get("held_on")
        fix = f"its {STABLE} sits on {where}" if where else f"deposit {STABLE} there"
        return (f"🚧 {trade['label']} — not all opened, Hyperliquid is ${trade['usd_value']:.2f} "
                f"{STABLE} short ({fix})")
    if status == "unavailable":
        where = trade.get("held_on") or "another venue"
        return (f"🚧 {symbol} — ${trade['usd_value']:.2f} of the {side.lower()} leg is not "
                f"executable on {trade.get('venue', 'the venue')}, it sits on {where}")
    if status == "error":
        return f"❌ {side} {symbol}: trade failed (see logs)"
    if status == "dry_run":
        return f"📋 {side} {qty} {symbol}"
    return f"✅ {side} {qty} {symbol} — id: {trade.get('id', '?')}"


def _trade_valuation(order: dict, prices: dict) -> tuple[float | None, float | None]:
    """USD (price, value) of a filled order, from the fill where the venue reports it.

    Against STABLE the fill's average price and cost are already USD. A cross-pair
    fill is priced in its quote coin (ETH/BTC in BTC), so it falls back to the
    rebalance-time USD price of the base.
    """
    base, _, quote = (order.get("symbol") or "").partition("/")
    amount = order.get("amount") or 0
    # A perp is quoted "USDC:USDC", the second half naming the settlement coin.
    if quote.split(":")[0] == STABLE:
        price = order.get("average") or order.get("price") or prices.get(base)
        usd_value = order.get("cost") or (amount * price if price else None)
    else:
        price = prices.get(base)
        usd_value = amount * price if price else None
    return price, usd_value


def _unattempted_legs(legs, reason: str) -> list:
    return [{"symbol": f"{token}/{STABLE}", "side": side, "amount": amount, "error": reason}
            for side, planned in legs
            for token, amount in planned.items()]


class Portfolio:
    def __init__(self):
        self.summary: Summary = Summary()
        self.balance: Balance = Balance()
        self.targets: dict = load_json("config/targets.json")
        self.balance.track_symbols(self.targets)
        self.venues: dict = self.balance.get_venue_balances()
        self.holdings: dict = Balance.aggregate(self.venues)
        self.send_rebalance: bool = False

    def get_targets(self) -> dict:
        return self.targets

    def set_target(self, symbol: str, percent: int) -> dict:
        self.targets[symbol] = percent
        logger.debug("Target for %s set to %d%%", symbol, percent)
        return self.targets

    def untracked_targets(self) -> dict[str, float]:
        """Targets with weight on a symbol the portfolio does not track.

        The rebalance plan only covers tracked symbols, so a signal naming anything
        else — NEAR, before it was tracked — had its share dropped without a word and
        left sitting in the stable coin. Callers surface this instead.
        """
        return {symbol: pct for symbol, pct in self.targets.items()
                if pct and symbol not in self.holdings}

    def _untracked_notice(self) -> str | None:
        untracked = self.untracked_targets()
        if not untracked:
            return None
        listed = ", ".join(f"{symbol} {pct}%" for symbol, pct in untracked.items())
        logger.error("Targets on untracked symbols are not rebalanced: %s", listed)
        return f"⚠️ Not tracked by the bot, left in {STABLE}: {listed}"

    def update_portfolio(self) -> None:
        self.balance.refresh_binance_balances()
        # After the refresh, which would clear a degraded flag a failed listing check sets.
        # Every target key counts, including 0%: a token the latest signal dropped is
        # still held until the rebalance sells it.
        self.balance.track_symbols(self.targets)
        self.venues = self.balance.get_venue_balances()
        self.holdings = Balance.aggregate(self.venues)
        logger.debug("Portfolio updated: %s", self.holdings)

    def _held_off_venue(self, token: str, venue: str) -> dict[str, float]:
        """How much of `token` sits on venues other than `venue`, keyed by venue.

        Empty when the per-venue breakdown is unavailable, so callers report nothing
        rather than guessing at a location for a shortfall they cannot explain.
        """
        venues = getattr(self, "venues", None) or {}
        return {name: held.get(token, 0.0) for name, held in venues.items()
                if name != venue and held.get(token, 0.0) > 0}

    def _unavailable_leg(self, symbol: str, side: str, token: str, amount: float,
                         usd_value: float, venue: str) -> dict | None:
        """A leg the plan wanted but `venue` cannot fill, because the coins are elsewhere.

        Reported only for the part actually traceable to another venue — a shortfall
        from a stale snapshot or an open order is not a transfer problem and would just
        be noise."""
        elsewhere = self._held_off_venue(token, venue)
        stranded = min(amount, sum(elsewhere.values()))
        if stranded <= 0:
            return None
        return {"symbol": symbol, "side": side, "amount": stranded, "venue": venue,
                "usd_value": usd_value * stranded / amount if amount else 0.0,
                "held_on": ", ".join(sorted(elsewhere)), "unavailable": True}

    def fetch_live_data(self) -> tuple[dict, dict, float]:
        prices = fetch_prices(list(self.holdings))
        values = {symbol: amount * prices[symbol] for symbol, amount in self.holdings.items()}
        total_value = sum(values.values())
        logger.debug("Total: %s", total_value)
        return prices, values, total_value

    def evaluate_symbol(self, values: dict, total_value: float) -> None:
        for symbol, value in values.items():
            current_pct = (value / total_value) * 100
            target_pct = float(self.targets[symbol])
            diff = current_pct - target_pct
            arrow = "🔺" if diff > 0 else "🔻"

            if current_pct >= SUMMARY_MIN_PCT or target_pct >= SUMMARY_MIN_PCT:
                self.summary.add_summary(
                    f"${symbol}: {current_pct:.2f}% (Target: {target_pct:.2f}%) {arrow} {diff:.2f}%"
                )

            if abs(diff) > REBALANCE_THRESHOLD_PCT:
                self.summary.add_rebalance(
                    f"⚠️ *Rebalance Needed*: ${symbol} is off by {diff:+.2f}% "
                    f"(Current: {current_pct:.2f}%; Target: {target_pct:.2f}%)"
                )
                self.send_rebalance = True

    def evaluate_shorts(self, total_value: float) -> None:
        """Report each short held on Hyperliquid against the latest signal's.

        The targets file holds long weights only, and the margin behind a short is
        counted as USDC, so without this /check shows an all-USDC book as balanced
        whatever the shorts are doing. A read failure is reported, never raised:
        the longs above it are still worth showing.
        """
        shorts, notice = self._latest_shorts()
        if shorts is None:
            if notice:
                self.summary.add_summary(f"\n{notice}")
            return
        if not HYPERLIQUID_ACCOUNT_ADDRESS or not HYPERLIQUID_PRIVATE_KEY:
            self.summary.add_summary("\n⚠️ Hyperliquid credentials not set — shorts not shown.")
            return
        try:
            exchange = create_hyperliquid(HYPERLIQUID_ACCOUNT_ADDRESS, HYPERLIQUID_PRIVATE_KEY)
            symbols = {coin: perp_symbol(exchange, coin) for coin in shorts}
            held_usd = self._held_shorts_usd(exchange, symbols)
        except Exception as err:
            logger.exception("Could not read Hyperliquid shorts: %s", err)
            self.summary.add_summary("\n⚠️ Could not read Hyperliquid shorts. Check logs for details.")
            return
        if not shorts and not held_usd:
            return

        self.summary.add_summary("\n🔻 *Shorts*")
        for coin in sorted(shorts.keys() | held_usd.keys()):
            if coin in symbols and symbols[coin] is None:
                self.summary.add_summary(f"${coin}: no Hyperliquid perp — held in {STABLE}")
                continue
            self._report_short(coin, held_usd.get(coin, 0.0), shorts.get(coin, 0.0), total_value)

    def _report_short(self, coin: str, held_usd: float, target_pct: float, total_value: float) -> None:
        current_pct = held_usd / total_value * 100 if total_value else 0.0
        diff = current_pct - target_pct
        arrow = "🔺" if diff > 0 else "🔻"
        self.summary.add_summary(
            f"${coin}: {current_pct:.2f}% short (Target: {target_pct:.2f}%) {arrow} {diff:.2f}%"
        )
        if abs(diff) > REBALANCE_THRESHOLD_PCT:
            self.summary.add_rebalance(
                f"⚠️ *Rebalance Needed*: ${coin} short is off by {diff:+.2f}% "
                f"(Current: {current_pct:.2f}%; Target: {target_pct:.2f}%)"
            )
            self.send_rebalance = True

    def perp_lines(self) -> list[str]:
        """/balance's perp section: each open Hyperliquid perp's size and side.

        Empty when the bot trades no perps or none are open; a notice when they
        cannot be read, so the balances above it are still shown.
        """
        if not (constants.on_hyperliquid() or constants.SHORTS_ENABLED):
            return []
        if not HYPERLIQUID_ACCOUNT_ADDRESS or not HYPERLIQUID_PRIVATE_KEY:
            return ["⚠️ Hyperliquid credentials not set — perps not shown."]
        try:
            exchange = create_hyperliquid(HYPERLIQUID_ACCOUNT_ADDRESS, HYPERLIQUID_PRIVATE_KEY)
            positions = [(p["symbol"], float(p.get("contracts") or 0), p.get("side"))
                         for p in exchange.fetch_positions()]
        except Exception as err:
            logger.exception("Could not read Hyperliquid perps: %s", err)
            return ["⚠️ Could not read Hyperliquid perps. Check logs for details."]
        return [f"{symbol.split('/')[0]}: {contracts} {side}"
                for symbol, contracts, side in sorted(positions) if contracts]

    @staticmethod
    def _held_shorts_usd(exchange, symbols: dict[str, str | None]) -> dict[str, float]:
        """Open shorts as {coin: USD notional at the mid}.

        A coin Hyperliquid lists per thousand (SHIB as kSHIB) is named as the signal
        names it, also when the signal no longer names it: the perp's own name says
        whether it is one, where ccxt's upper-cased base (KSHIB, but also KAS) cannot.
        """
        coins = {symbol: coin for coin, symbol in symbols.items() if symbol}
        held = {}
        for symbol, contracts in held_shorts(exchange).items():
            price = mid_price(exchange, symbol)
            if price is None:
                raise ValueError(f"no price on Hyperliquid for {symbol}")
            info = exchange.markets[symbol].get("info") or {}
            name = coins.get(symbol) or perp_coin(info.get("name") or symbol.split("/")[0])[0]
            held[name] = contracts * price
        return held

    def _compute_rebalance(self, prices: dict, values: dict, total_value: float) -> dict[str, float]:
        usable_value = total_value * (1 - REBALANCE_RESERVE_PCT / 100)
        return {
            symbol: ((self.targets[symbol] / 100) * usable_value - values[symbol]) / prices[symbol]
                    if prices[symbol] > 0 else 0.0
            for symbol in self.holdings
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
        self.evaluate_shorts(total_value)
        untracked = self._untracked_notice()
        if untracked:
            self.summary.add_summary(f"\n{untracked}")
        if self.send_rebalance:
            self.calculate_rebalance(prices, values, total_value)

        degraded = self.balance.degraded
        if degraded:
            venues = ", ".join(sorted(degraded))
            logger.error("Balances degraded (%s) — recording snapshot as partial", venues)
            self.summary.add_summary(f"\n⚠️ Partial snapshot — balance fetch failed ({venues})")

        # Recorded either way: a partial row keeps the audit trail without ever
        # being read back as a baseline (see data.database._clean_snapshot).
        record_snapshot(
            signal_id=get_latest_signal_id(),
            total_value_usd=total_value,
            balances=self.holdings,
            prices=prices,
            values_usd=values,
            targets=self.targets,
            partial=bool(degraded),
        )
        return self.summary.flush_summary()

    def _plan_trades(self, rebalance: dict, prices: dict) -> tuple[dict, dict, list]:
        """Classify rebalance amounts into sells, buys, and non-executed (dust/manual) trades."""
        sells, buys, skipped = {}, {}, []
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
                skipped.append({"symbol": symbol, "side": side, "amount": amount, "usd_value": usd_value, "dust": True})
            elif symbol in MANUAL_ASSETS and not constants.on_hyperliquid():
                logger.info("Trade for %s ($%.2f) is manual-only — no bot venue trades it", symbol, usd_value)
                skipped.append({"symbol": symbol, "side": side, "amount": abs(amount),
                                "usd_value": usd_value, "manual": True})
            elif amount < 0:
                sells[symbol] = abs(amount)
            else:
                buys[symbol] = amount
        return sells, buys, skipped

    def _execute_cross_pairs(self, exchange, sells: dict, buys: dict, prices: dict, dry_run: bool) -> list:
        """Match sell/buy legs via direct cross-pairs (e.g. ETH/BTC) before routing through STABLE.
        Mutates `sells` and `buys` to reduce or remove fully-matched legs."""
        results = []
        if not sells or not buys:
            return results
        free = dict(exchange.fetch_balance().get("free", {}))

        # sells loses fully-matched legs inside the loop, so iterate over a copy.
        for sell_token in list(sells):  # NOSONAR S7504: copy needed, sells is mutated
            results.extend(self._match_sell_leg(exchange, sell_token, sells, buys, prices, free, dry_run))
        return results

    def _match_sell_leg(self, exchange, sell_token: str, sells: dict, buys: dict, prices: dict,
                        free: dict, dry_run: bool) -> list:
        """Cross-trade one sell leg against each buy leg until the sell leg is used up."""
        results = []
        # buys loses fully-matched legs inside the loop, so iterate over a copy.
        for buy_token in list(buys):  # NOSONAR S7504: copy needed, buys is mutated
            if sell_token == buy_token:
                continue
            leg = self._plan_cross_leg(exchange, sell_token, buy_token, sells, buys, prices, free)
            if leg is None:
                continue

            symbol, side, amount, actual_sell, cross_min = leg
            try:
                order = place_order(exchange, symbol, side, amount, dry_run)
            except Exception as err:
                logger.error("Cross trade error %s->%s via %s: %s", sell_token, buy_token, symbol, err)
                results.append({"symbol": symbol, "side": side, "amount": amount, "error": str(err)})
                continue

            results.append(order)
            if self._settle_cross_leg(sell_token, buy_token, actual_sell, cross_min,
                                      sells, buys, prices, free):
                break
        return results

    def _plan_cross_leg(self, exchange, sell_token: str, buy_token: str, sells: dict, buys: dict,
                        prices: dict, free: dict) -> tuple | None:
        """Size a direct cross-trade between two legs, or None when no worthwhile trade exists.

        Returns (symbol, side, order_amount, sell_token_amount, min_usd)."""
        direct = find_direct_pair(exchange, sell_token, buy_token)
        if not direct:
            return None

        symbol, side = direct
        cross_min = effective_min_usd(exchange, symbol, MIN_TRADE_USD)
        matched_usd = min(
            sells[sell_token] * prices[sell_token],
            buys[buy_token] * prices[buy_token],
        )
        if matched_usd < cross_min:
            return None

        # Free balance on this venue is the authority on what can settle here;
        # the aggregate portfolio may count coins sitting on Kraken or Arbitrum.
        free_amount = float(free.get(sell_token, 0.0))
        if free_amount <= 0:
            return None
        actual_sell = min(matched_usd / prices[sell_token], free_amount)
        if actual_sell <= 0 or actual_sell * prices[sell_token] < cross_min:
            return None  # not enough free balance to make this a non-dust cross-trade

        if side == "sell":
            order_amount = actual_sell
        else:
            order_amount = actual_sell * prices[sell_token] / prices[buy_token]
        try:
            amount = apply_precision(exchange, symbol, order_amount)
        except Exception as err:
            logger.warning("Cross precision error %s->%s via %s: %s", sell_token, buy_token, symbol, err)
            return None
        if amount <= 0:
            return None
        return symbol, side, amount, actual_sell, cross_min

    @staticmethod
    def _settle_cross_leg(sell_token: str, buy_token: str, actual_sell: float, cross_min: float,
                          sells: dict, buys: dict, prices: dict, free: dict) -> bool:
        """Deduct a filled cross-trade from both legs. True when the sell leg is used up."""
        executed_usd = actual_sell * prices[sell_token]
        sells[sell_token] = max(0.0, sells[sell_token] - executed_usd / prices[sell_token])
        buys[buy_token] = max(0.0, buys[buy_token] - executed_usd / prices[buy_token])
        free[sell_token] = float(free.get(sell_token, 0.0)) - actual_sell
        # A remainder too small for another cross-trade stays in the plan, so
        # the USDC route either trades it or reports it as dust; only a fully
        # matched leg is removed.
        if buys[buy_token] * prices[buy_token] < MATCHED_USD:
            buys.pop(buy_token, None)
        if sells[sell_token] * prices[sell_token] < MATCHED_USD:
            sells.pop(sell_token, None)
            return True  # this sell_token is done; move to the next
        # Nothing left of this sell_token that another cross could take.
        return sells[sell_token] * prices[sell_token] < cross_min

    def _execute_sells(self, exchange, sells: dict, prices: dict, dry_run: bool) -> list:
        """Sell each token's planned amount, capped at what this venue holds free.

        The plan is sized against the aggregate portfolio, but only the free balance on
        this exchange can settle — the rest of the position may be on Kraken or Arbitrum
        with no transfer path to here. Capping (rather than scaling by the aggregate)
        sells the full intent whenever the venue can cover it, and the exact remainder
        is reported instead of quietly shrinking the trade.
        """
        results = []
        if not sells:
            return results
        free = exchange.fetch_balance().get("free", {})

        for token, planned_amount in sells.items():
            results.extend(self._sell_leg(
                exchange, token, planned_amount, float(free.get(token, 0.0)), prices, dry_run))
        return results

    def _sell_leg(self, exchange, token: str, planned_amount: float, free_amount: float,
                  prices: dict, dry_run: bool) -> list:
        """One token's sell, followed by a note for any part of it held on another venue."""
        symbol = f"{token}/{STABLE}"
        if not _is_directly_tradeable(exchange, token, STABLE):
            logger.info("Skipping %s — no Binance pair available", token)
            return [{"symbol": symbol, "side": "sell", "amount": planned_amount, "skipped": True}]

        sellable = min(planned_amount, max(free_amount, 0.0))
        price = prices.get(token, 0.0)
        shortfall = planned_amount - sellable
        stranded = self._unavailable_leg(symbol, "sell", token, shortfall,
                                         shortfall * price, Balance.BINANCE)
        tail = [stranded] if stranded else []

        if free_amount <= 0:
            logger.warning("No free %s balance to sell on this venue", token)
            return [{"symbol": symbol, "side": "sell", "amount": planned_amount,
                     "error": "zero balance"}] + tail

        sellable_usd = sellable * price
        floor = effective_min_usd(exchange, symbol, MIN_TRADE_USD)
        if sellable_usd < floor:
            logger.info("Sellable %s ($%.2f) below minimum $%.2f — dust", token, sellable_usd, floor)
            return [{"symbol": symbol, "side": "sell", "amount": sellable,
                     "usd_value": sellable_usd, "min_usd": floor, "dust": True}] + tail
        try:
            sell_amount = apply_precision(exchange, symbol, sellable)
        except Exception as err:
            logger.warning("Sell precision error for %s (free=%s sellable=%s): %s",
                           token, free_amount, sellable, err)
            return [{"symbol": symbol, "side": "sell", "amount": 0,
                     "error": ERR_SIZE_BELOW_PRECISION}] + tail
        if sell_amount <= 0:
            logger.warning("Sell size for %s rounded to 0 (free=%s sellable=%s)",
                           token, free_amount, sellable)
            return [{"symbol": symbol, "side": "sell", "amount": 0,
                     "error": ERR_SIZE_BELOW_PRECISION}] + tail

        try:
            return [place_order(exchange, symbol, "sell", sell_amount, dry_run)] + tail
        except Exception as err:
            logger.error("Trade error on sell %s: %s", token, err)
            return [{"symbol": symbol, "side": "sell", "amount": sell_amount,
                     "error": str(err)}] + tail

    def _execute_buys(self, exchange, buys: dict, prices: dict, dry_run: bool) -> list:
        """Spend per-token fraction of fresh free USDC (quoteOrderQty) weighted by intended USD."""
        results = []
        if not buys:
            return results
        free = exchange.fetch_balance().get("free", {})
        free_stable = float(free.get(STABLE, 0.0))

        total_intended_usd = sum(amt * prices[tok] for tok, amt in buys.items())
        # The plan was sized against STABLE pooled across every venue, but only this
        # exchange's balance can fund these orders and there is no transfer path to it.
        # Name the difference, rather than letting the shortfall look like a smaller plan.
        shortfall_usd = max(0.0, total_intended_usd - free_stable)
        stranded = self._unavailable_leg(STABLE, "buy", STABLE, shortfall_usd,
                                         shortfall_usd, Balance.BINANCE)

        if total_intended_usd <= 0 or free_stable <= 0:
            for token, amount in buys.items():
                results.append({"symbol": f"{STABLE}/{token}", "side": "buy", "amount": amount,
                                "error": f"no {STABLE} to spend"})
            if stranded:
                results.append(stranded)
            return results

        # Spend each token's intended USD, scaling the whole plan down proportionally
        # only when free stable can't cover it. Without the cap, a single buy leg would
        # consume the entire free balance regardless of its target allocation.
        scale = min(1.0, free_stable / total_intended_usd)
        if stranded:
            results.append(stranded)

        for token, planned_amount in buys.items():
            pair_display = f"{STABLE}/{token}"
            if not _is_directly_tradeable(exchange, token, STABLE):
                logger.info("Skipping %s — no Binance pair available", token)
                results.append({"symbol": pair_display, "side": "buy", "amount": planned_amount, "skipped": True})
                continue

            intended_usd = planned_amount * prices[token]
            cost = intended_usd * scale
            symbol = f"{token}/{STABLE}"
            floor = effective_min_usd(exchange, symbol, MIN_TRADE_USD)
            if cost < floor:
                logger.info("Buy for %s ($%.2f) below minimum $%.2f — dust", token, cost, floor)
                results.append({"symbol": pair_display, "side": "buy", "amount": planned_amount,
                                "usd_value": cost, "min_usd": floor, "dust": True})
                continue
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
            if status == "filled":
                price, usd_value = _trade_valuation(trade, prices)
            else:
                # Nothing traded, so there is no fill to value.
                price, usd_value = prices.get((trade.get("symbol") or "").split("/")[0]), None
            record_trade(
                signal_id=signal_id,
                symbol=trade.get("symbol") or "",
                side=trade.get("side") or "",
                amount=trade.get("amount", 0),
                price=price,
                usd_value=usd_value,
                status=status,
                order_id=trade.get("id"),
                dry_run=False,
                fee_amount=trade.get("fee_amount"),
                fee_currency=trade.get("fee_currency"),
                fee_rate=trade.get("fee_rate"),
            )

    def _refusal(self) -> str | None:
        """Why the rebalance cannot trade at all, or None when it can."""
        if constants.on_hyperliquid():
            if not (HYPERLIQUID_ACCOUNT_ADDRESS and HYPERLIQUID_PRIVATE_KEY):
                return "⚠️ Hyperliquid credentials not set — cannot execute trades."
        elif not BINANCE_API_KEY or not BINANCE_API_SECRET:
            return "⚠️ Binance API credentials not set — cannot execute trades."

        self.update_portfolio()
        if self.balance.degraded:
            # A venue that failed to answer reports 0.0, so its holdings look sold
            # already. Rebalancing on that sells the rest of the book down to match
            # a portfolio that does not exist.
            venues = ", ".join(sorted(self.balance.degraded))
            logger.error("Refusing to rebalance: balances degraded (%s)", venues)
            return f"⚠️ Balance fetch failed ({venues}) — refusing to trade on incomplete holdings."

        missing = sorted(set(self.holdings) - set(self.targets))
        if missing:
            # A tracked symbol with no target would raise KeyError mid-plan; treating
            # it as 0% instead would sell the whole position on a config typo.
            logger.error("Refusing to rebalance: no target for %s", ", ".join(missing))
            return (f"⚠️ No target set for {', '.join(missing)} in config/targets.json "
                    f"— refusing to rebalance.")
        return None

    def execute_rebalance(self, dry_run: bool = True) -> str:
        refusal = self._refusal()
        if refusal:
            return refusal

        prices, values, total_value = self.fetch_live_data()
        rebalance = self._compute_rebalance(prices, values, total_value)

        sells, buys, skipped = self._plan_trades(rebalance, prices)
        untracked = self._untracked_notice()
        traded, venue_notices, shorts_acted = self._trade(sells, buys, prices, total_value, dry_run)
        results = skipped + traded
        notices = [n for n in (untracked, *venue_notices) if n]

        if not sells and not buys and not shorts_acted and not venue_notices:
            if results:
                lines = ["🔄 *Rebalance — nothing executable*\n"] + [_format_trade_line(t) for t in results]
            else:
                lines = ["✅ Portfolio is balanced — no trades needed."]
            return "\n".join(lines + notices)

        if not dry_run:
            self._persist_trades(results, prices)

        mode = "DRY RUN" if dry_run else "LIVE"
        lines = [f"🔄 *Rebalance {mode}*\n"]
        lines += notices
        lines += [_format_trade_line(t) for t in results]
        return "\n".join(lines)

    def _trade(self, sells: dict, buys: dict, prices: dict, total_value: float,
               dry_run: bool) -> tuple[list, list, bool]:
        """Send the legs to their venues. Returns (results, venue notices, whether a short moved)."""
        on_hyperliquid = constants.on_hyperliquid()
        results, notices, shorts_acted = [], [], False
        if (sells or buys) and not on_hyperliquid:
            binance_results, notice = self._execute_binance(sells, buys, prices, dry_run)
            results += binance_results
            notices.append(notice)
        if on_hyperliquid or constants.SHORTS_ENABLED:
            # Off Hyperliquid, the longs went to Binance above and only the shorts are left.
            longs = (sells, buys) if on_hyperliquid else ({}, {})
            hyperliquid_results, notice = self._execute_hyperliquid(*longs, prices, total_value, dry_run)
            results += hyperliquid_results
            notices.append(notice)
            shorts_acted = any(_trade_status(t) != "dust" for t in hyperliquid_results)
        return results, [n for n in notices if n], shorts_acted

    def _execute_hyperliquid(self, sells: dict, buys: dict, prices: dict, total_value: float,
                             dry_run: bool) -> tuple[list, str | None]:
        """Trade Hyperliquid toward the longs given and the latest signal's shorts.

        Returns (results, notice). Every failure before an order goes out leaves the
        positions as they are: reading a failure as "no shorts" would cover them all.
        """
        legs = (("sell", sells), ("buy", buys))
        if not HYPERLIQUID_ACCOUNT_ADDRESS or not HYPERLIQUID_PRIVATE_KEY:
            logger.error("SHORTS_ENABLED is set but Hyperliquid credentials are missing")
            return [], "⚠️ Hyperliquid credentials not set — shorts left as they are."

        shorts, notice = self._latest_shorts()
        if shorts is None and not sells and not buys:
            return [], notice

        try:
            exchange = create_hyperliquid(HYPERLIQUID_ACCOUNT_ADDRESS, HYPERLIQUID_PRIVATE_KEY)
            exits, entries, results = self._plan_hyperliquid(exchange, sells, buys, prices,
                                                             shorts, total_value)
        except UnsupportedAccountMode as err:
            logger.error("Refusing to trade Hyperliquid: %s", err)
            return _unattempted_legs(legs, str(err)), f"⚠️ {err}. Nothing traded."
        except Exception as err:
            logger.exception("Hyperliquid unreachable: %s", err)
            unattempted = _unattempted_legs(legs, f"Hyperliquid unreachable: {err}")
            notice = "⚠️ Failed to reach Hyperliquid — positions left as they are. Check logs for details."
            return unattempted, notice

        placed, short_usdc = execute_legs(exchange, exits, entries, dry_run)
        results += placed
        if short_usdc >= MATCHED_USD:
            results.append(self._unfunded_leg(short_usdc, bool(buys), bool(shorts)))
        return results, notice

    @staticmethod
    def _latest_shorts() -> tuple[dict | None, str | None]:
        """The latest signal's shorts and a notice; None when they are not to be touched.

        They come from the stored signal rather than the targets file, which holds
        long weights only.
        """
        if not constants.SHORTS_ENABLED:
            return None, None
        try:
            return split_shorts(get_latest_allocations() or {})[1], None
        except Exception as err:
            logger.exception("Could not read the latest signal's shorts: %s", err)
            return None, "⚠️ Could not read the latest signal — shorts left as they are."

    def _plan_hyperliquid(self, exchange, sells: dict, buys: dict, prices: dict,
                          shorts: dict | None, total_value: float) -> tuple[list, list, list]:
        """(exits, entries, results) for the longs and, unless None, the shorts."""
        exits, entries, results = [], [], []
        if sells or buys:
            mode, _, _ = account_state(exchange)
            if mode != UNIFIED:
                # Spot buys and perp margin draw on one USDC balance only in a
                # unified account; the bot cannot move USDC between them otherwise.
                raise UnsupportedAccountMode(
                    "Hyperliquid account is not Unified — switch it in Hyperliquid's settings")
            closing = {coin for coin, pct in self.targets.items() if not pct}
            exits, entries, results = plan_longs(exchange, sells, buys, prices, closing)
        if shorts is not None:
            book_usd = total_value * (1 - REBALANCE_RESERVE_PCT / 100)
            covers, opens, skipped = plan_shorts(exchange, shorts, book_usd)
            exits, entries, results = exits + covers, entries + opens, results + skipped
        return exits, entries, results

    def _unfunded_leg(self, usd: float, buys: bool, shorts: bool) -> dict:
        """The USDC the buys and opens fell short by, and where that USDC sits."""
        if buys and shorts:
            label = "Buys and shorts"
        else:
            label = "Buys" if buys else "Shorts"
        elsewhere = self._held_off_venue(STABLE, Balance.HYPERLIQUID)
        return {"symbol": STABLE, "side": "sell", "amount": usd, "usd_value": usd,
                "venue": Balance.HYPERLIQUID, "label": label,
                "held_on": ", ".join(sorted(elsewhere)), "unavailable": True}

    def _execute_binance(self, sells: dict, buys: dict, prices: dict,
                         dry_run: bool) -> tuple[list, str | None]:
        """Run the Binance legs, returning (results, notice).

        An unreachable Binance is reported per leg rather than by returning early, so
        every planned leg still reaches _persist_trades and the message the user sees.
        """
        try:
            exchange = create_binance(BINANCE_API_KEY, BINANCE_API_SECRET)
        except Exception as err:
            logger.error("Failed to connect to Binance: %s", err)
            unattempted = _unattempted_legs((("sell", sells), ("buy", buys)),
                                            f"Binance unreachable: {err}")
            leg_count = len(unattempted)
            return unattempted, f"⚠️ Failed to connect to Binance — {leg_count} leg(s) not attempted. Check logs for details."

        # Each stage opens with a fetch_balance(). One that fails leaves the legs it
        # and later stages would have traded unattempted, but the orders earlier
        # stages already placed must still reach the results.
        results = []
        stages = (
            (lambda: self._execute_cross_pairs(exchange, sells, buys, prices, dry_run),
             (("sell", sells), ("buy", buys))),
            (lambda: self._execute_sells(exchange, sells, prices, dry_run),
             (("sell", sells), ("buy", buys))),
            (lambda: self._execute_buys(exchange, buys, prices, dry_run),
             (("buy", buys),)),
        )
        for run, remaining in stages:
            try:
                results.extend(run())
            except Exception as err:
                logger.exception("Binance stage failed: %s", err)
                unattempted = _unattempted_legs(remaining, f"Binance error: {err}")
                notice = f"⚠️ Binance failed mid-rebalance — {len(unattempted)} leg(s) not attempted. Check logs for details."
                return results + unattempted, notice
        return results, None
