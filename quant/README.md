# Paper quant overlay (v1 / PR2)

PAPER-only scaffold for a research / paper-trading quant path beside the
existing Telegram rebalance bot.

## What this is

- **Binance public OHLCV** via ccxt (`PublicCcxtCandleSource`) -- **no API keys**
- Helpers: `candles_from_ccxt_rows`, `closes`, `log_returns`, `FixtureCandleSource`
- **Momentum + vol-band signal** (`MomentumVolSignal`) -- score only, no orders
- **In-memory paper ledger** starting at **10_000 USDT** virtual equity
- **Headless runner** that logs mode + equity / PnL and **does not trade**
- **Execution lock**: `EXECUTION_MODE` defaults to `PAPER`; LIVE from
  automation raises and must never call live order APIs

## Momentum / vol (PR2)

Documented formula -- **not an alpha claim**:

1. `momentum_return = close[-1] / close[-lookback] - 1` (default lookback 24 on `1h`)
2. `score = clip(momentum_return / 0.05, -1, 1)`
3. Realized vol = stdev of log returns over `vol_lookback` (default 24)
4. If vol outside `[min_vol, max_vol]` -> score/confidence 0 (`reason=vol_filter`)
5. Need `max(lookback, vol_lookback) + 1` candles; else `insufficient_data`

Tests mock the exchange or use fixtures -- **no network** required.

## What this is not

This package **does not promise alpha**. Paper fills / sizing remain limited;
LIVE stays locked. It does **not** modify `portfolio.py` or live
`data/trading.py` paths.

## Layout

```
quant/
  config.py           # universe, timeframe, risk caps, PAPER defaults
  research/candles.py # public OHLCV + helpers / fixtures
  signals/            # SignalProtocol + MomentumVolSignal
  risk/engine.py      # kill-switch + degraded-venue refuse
  execution/          # PaperBroker + ExecutionRouter
  runner.py           # headless one-shot (log only)
  pnl.py              # paper equity / PnL summary
```

## Run (no network needed for the runner)

```bash
python -m quant.runner
# or: EXECUTION_MODE=PAPER python -m quant.runner
```

## Tests

```bash
pytest tests/quant -q
```
