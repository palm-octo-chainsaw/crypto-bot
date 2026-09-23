# Paper quant overlay (v1)

PAPER-only scaffold for a research / paper-trading quant path beside the
existing Telegram rebalance bot.

## What this is

- **Binance public OHLCV** interface stub (ccxt) — no API keys for v1
- **In-memory paper ledger** starting at **10_000 USDT** virtual equity
- **Headless runner** that logs mode + equity / PnL and **does not trade**
- **Execution lock**: `EXECUTION_MODE` defaults to `PAPER`; LIVE from
  automation raises and must never call live order APIs

## What this is not

This package **does not promise alpha**. Momentum / vol filters, fills,
and sizing are stubs or TODOs for PR2/PR3. It does **not** modify
`portfolio.py` or live `data/trading.py` paths.

## Layout

```
quant/
  config.py           # universe, timeframe, risk caps, PAPER defaults
  research/candles.py # OHLCV interface (TODO PR2)
  signals/            # SignalProtocol + momentum stub
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
