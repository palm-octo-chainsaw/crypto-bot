"""One-time backfill of the columns added after v1.15.1.

Rows written before the release that records them have NULL in trades.direction,
trades.perp, trades.opened/closed and the fx_currency/fx_rate pair on trades and
snapshots. This fills what can still be known:

- perp: from the symbol, since a perp names its settlement coin (SOL/USDC:USDC).
- direction: a spot trade is always long. A perp's is read from Hyperliquid's own
  record of the order's fills (Open Short, Close Long, ...), matched by order id.
- opened/closed: from symbol, side and direction, once the direction is known.
- fx_currency/fx_rate: the current display currency at the ECB rate of the row's
  day, so an old amount reads as it did then.

Only NULLs are written, so re-running is safe. A perp trade Hyperliquid has no fill
for (a failed order, or one older than the fills it keeps) is reported and left.

Run it after the release has started once, so init_db has added the columns:

    python scripts/backfill_new_columns.py           # report what would change
    python scripts/backfill_new_columns.py --apply   # write it
"""
import bisect
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests
from psycopg import sql
from psycopg.rows import dict_row

# Allow running directly (`python scripts/backfill_new_columns.py`) by putting the
# repo root on the path so `data` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import constants
from data import fx
from data.database import get_connection, opened_closed

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
# userFillsByTime returns at most this many fills per call.
FILLS_PAGE = 2000
DIRECTIONS = {"Open Long": "long", "Close Long": "long", "Open Short": "short", "Close Short": "short"}


def hyperliquid_directions(address: str, since: datetime) -> dict[str, str]:
    """{order id: 'long' or 'short'} for the account's perp fills since `since`.

    A fill that flips a position (Long > Short) has no single direction and is left out.
    """
    directions: dict[str, str] = {}
    start = int(since.timestamp() * 1000)
    while True:
        response = requests.post(HYPERLIQUID_INFO_URL, timeout=15, json={
            "type": "userFillsByTime", "user": address, "startTime": start})
        response.raise_for_status()
        fills = response.json()
        for fill in fills:
            if fill.get("dir") in DIRECTIONS:
                directions.setdefault(str(fill["oid"]), DIRECTIONS[fill["dir"]])
        latest = max((fill["time"] for fill in fills), default=start)
        if len(fills) < FILLS_PAGE or latest == start:
            return directions
        # Resume from the last fill's millisecond: fills sharing it come back again,
        # and setdefault keeps the first answer.
        start = latest


def daily_usd_rates(code: str, first: date, last: date) -> list[tuple[date, float]]:
    """ECB rates of `code` per USD from shortly before `first` to `last`, oldest first.

    The ECB publishes on working days only, so a week is asked for before `first`
    to give a weekend or holiday row the last rate before it.
    """
    if code == fx.BASE:
        return [(first, 1.0)]
    base_url = constants.FX_RATES_URL.rsplit("/", 1)[0]
    response = requests.get(f"{base_url}/{first - timedelta(days=7)}..{last}",
                            params={"base": fx.BASE, "symbols": code}, timeout=15)
    response.raise_for_status()
    rates = response.json()["rates"]
    return sorted((date.fromisoformat(day), float(by_code[code])) for day, by_code in rates.items())


def rate_on(rates: list[tuple[date, float]], day: date) -> float:
    """The last published rate on or before `day`, else the earliest one."""
    index = bisect.bisect_right(rates, (day, float("inf"))) - 1
    return rates[max(index, 0)][1]


def plan_trade(trade: dict, directions: dict[str, str]) -> dict:
    """The NULL columns of one trade that can be filled, as {column: value}."""
    fill = {}
    is_perp = trade["perp"] if trade["perp"] is not None else ":" in trade["symbol"]
    if trade["perp"] is None:
        fill["perp"] = is_perp
    direction = trade["direction"]
    if direction is None:
        direction = directions.get(str(trade["order_id"])) if is_perp else "long"
        if direction:
            fill["direction"] = direction
    if direction and trade["opened"] is None and trade["closed"] is None:
        opened, closed = opened_closed(trade["symbol"], trade["side"], direction)
        if opened or closed:
            fill["opened"], fill["closed"] = opened, closed
    return fill


def _day(timestamp: datetime) -> date:
    return timestamp.astimezone(timezone.utc).date()


def _update(cur, table: str, row_id: int, fill: dict) -> None:
    """Write `fill` into the row's columns that are still NULL."""
    assignments = sql.SQL(", ").join(
        sql.SQL("{0} = COALESCE({0}, %s)").format(sql.Identifier(column)) for column in fill)
    cur.execute(sql.SQL("UPDATE {} SET {} WHERE id = %s").format(sql.Identifier(table), assignments),
                (*fill.values(), row_id))


def backfill(apply: bool) -> dict:
    with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        trades = cur.execute(
            """SELECT id, symbol, side, order_id, direction, perp, opened, closed, timestamp,
                      fx_rate IS NULL AS needs_rate
               FROM trades
               WHERE direction IS NULL OR perp IS NULL OR fx_rate IS NULL
                  OR (opened IS NULL AND closed IS NULL)"""
        ).fetchall()
        snapshots = cur.execute("SELECT id, timestamp FROM snapshots WHERE fx_rate IS NULL").fetchall()

        unknown_perps = [t for t in trades if t["direction"] is None
                         and (t["perp"] or (t["perp"] is None and ":" in t["symbol"]))]
        directions = {}
        if unknown_perps and not constants.HYPERLIQUID_ACCOUNT_ADDRESS:
            print("HYPERLIQUID_ACCOUNT_ADDRESS not set: perp directions left unknown")
        elif unknown_perps:
            directions = hyperliquid_directions(constants.HYPERLIQUID_ACCOUNT_ADDRESS,
                                                min(t["timestamp"] for t in unknown_perps))

        code = fx.current()
        days = [_day(t["timestamp"]) for t in trades if t["needs_rate"]]
        days += [_day(s["timestamp"]) for s in snapshots]
        rates = daily_usd_rates(code, min(days), max(days)) if days else []

        trade_fills = {}
        for trade in trades:
            fill = plan_trade(trade, directions)
            if trade["needs_rate"]:
                fill["fx_currency"] = code
                fill["fx_rate"] = rate_on(rates, _day(trade["timestamp"]))
            if fill:
                trade_fills[trade["id"]] = fill
        snapshot_fills = {s["id"]: {"fx_currency": code, "fx_rate": rate_on(rates, _day(s["timestamp"]))}
                          for s in snapshots}
        unresolved = [t["id"] for t in unknown_perps if "direction" not in trade_fills.get(t["id"], {})]

        if apply:
            for row_id, fill in trade_fills.items():
                _update(cur, "trades", row_id, fill)
            for row_id, fill in snapshot_fills.items():
                _update(cur, "snapshots", row_id, fill)

    return {"currency": code, "trades": len(trade_fills), "snapshots": len(snapshot_fills),
            "directions": sum("direction" in f for f in trade_fills.values()),
            "unresolved": unresolved}


if __name__ == "__main__":
    apply = "--apply" in sys.argv[1:]
    result = backfill(apply)
    verb = "Updated" if apply else "Would update"
    print(f"{verb} {result['trades']} trades ({result['directions']} directions) and "
          f"{result['snapshots']} snapshots, at {result['currency']} rates")
    if result["unresolved"]:
        print(f"Perp trades with no Hyperliquid fill, direction left NULL: {result['unresolved']}")
    if not apply:
        print("Dry run: nothing written. Re-run with --apply to write.")
