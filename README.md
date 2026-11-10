# crypto-telegram-bot

A Telegram bot for tracking a multi-source crypto portfolio, executing rebalancing trades on Binance, and automatically fetching signal allocations.

## Features

- **Multi-source balance aggregation** — combines on-chain wallets, Binance, and Kraken into a single portfolio view
- **Portfolio rebalancing** — computes per-asset deltas against target allocations and executes market orders on Binance
- **Signal scraping** — fetches RSPS signal allocations via headless browser with TOTP authentication, with timestamp-based deduplication
- **Stale signal detection** — normalizes relative timestamps and auto-clicks "Viewing older messages" to ensure the latest signal is fetched
- **Shutdown notifications** — sends a Telegram message when the bot stops
- **Dry-run mode** — preview trades before executing anything real
- **Dust filtering** — skips trades below a configurable USD minimum to avoid exchange errors
- **Leverage token tracking** — monitors ERC-20 leverage tokens on Arbitrum
- **Telegram interface** — all commands available via bot

## Supported Assets & Sources

| Asset | Sources |
|-------|---------|
| BTC | Binance + Kraken |
| ETH | Arbitrum (native) + Binance + Kraken |
| SOL | Binance + Kraken |
| SUI | Binance |
| XRP | Binance + Kraken |
| DOGE | Binance + Kraken |
| LINK | Binance + Kraken |
| BNB | Binance |
| USDC | Arbitrum (ERC-20) + Binance + Kraken |
| PAXG | Kraken |
| HYPE | Binance |

**Leverage tokens** (Arbitrum): `BTCBULL2X`, `BTCBULL4X`, `ETHBULL4X`

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

Create a `.env` file:

```env
# Telegram
BOT_TOKEN=your_telegram_bot_token
CHAT_ID=your_chat_id
# Who may run commands (optional, comma-separated Telegram user ids).
# When set, these users can also use the bot in a private chat.
ALLOWED_USER_IDS=123456789

# EVM wallet (Arbitrum token balances)
META_MASK=your_evm_wallet_address

# Binance (balance tracking + trade execution)
BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret

# Kraken (balance tracking only)
KRAKEN_API_KEY=your_kraken_api_key
KRAKEN_API_SECRET=your_kraken_api_secret

# Signal scraper (optional)
TRW_EMAIL=your_email
TRW_PASSWORD=your_password
TRW_TOTP_SECRET=your_totp_secret

# Discord (optional) — new signals are also posted to this channel webhook
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Trade settings (optional)
MIN_TRADE_USD=1.0

# Execution venue (optional, default binance) — see "The book on Hyperliquid" below
EXECUTION_VENUE=binance

# Shorts (optional, default false) — see "Shorts in signals" below
SHORTS_ENABLED=false
HYPERLIQUID_ACCOUNT_ADDRESS=your_hyperliquid_main_address
HYPERLIQUID_PRIVATE_KEY=your_hyperliquid_api_wallet_private_key
SHORT_LEVERAGE=1
```

### 3. Set target allocations

Edit `config/targets.json`:

```json
{
    "BTC": 50,
    "ETH": 30,
    "USDC": 20
}
```

Percentages do not need to sum to 100 — any unallocated portion is treated as no target.

### 4. Run

```bash
python run.py
```

#### Docker

```bash
docker build -t crypto-telegram-bot .
docker run --env-file .env crypto-telegram-bot
```

## Deployment

The bot runs on k3s as a single-replica Deployment. Its manifests are **not in
this repo** — the cluster is reconciled by ArgoCD from
[palm-octo-chainsaw/homelab-gitops](https://github.com/palm-octo-chainsaw/homelab-gitops),
under `manifests/crypto-bot/`. `secret.example.yaml` there documents every
environment variable the bot expects.

A `v*.*.*` tag push builds the image, commits the new tag into that repo, and
ArgoCD rolls it out; CI then waits for the rollout and fails the release if the
bot does not come up. Nothing runs `kubectl set image` any more.

`scripts/migrate_sqlite_to_pg.py` is retained from the one-time SQLite →
Postgres cutover; the procedure it belonged to is in this repo's git history.

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/check` | Show current allocation vs targets, flag assets that need rebalancing |
| `/balance` | Show raw spot balances across all sources |
| `/leverage` | Show leverage token balances |
| `/total` | Show total portfolio value in USD |
| `/get_targets` | Show current target allocations |
| `/set_target SYMBOL PCT` | Update a target (e.g. `/set_target BTC 40`) |
| `/rebalance` | Preview rebalancing trades (dry run) |
| `/rebalance live` | Execute real market orders on Binance |
| `/fetch_signal` | Fetch latest RSPS signal and update targets |
| `/status` | Show scheduled poller status |
| `/performance [24h\|7d\|30d\|all]` | Portfolio value change over a window |

## Degraded balance reads

Every venue fetcher in `data/balance.py` falls back to `0.0` when its API call fails, so one dead
venue cannot stop the rest of the portfolio from being priced. That fallback is indistinguishable
from an empty wallet, so `Balance.degraded` records which venues could not be read, and two things
follow from it:

- **Snapshots** are still written, but marked `partial`. Partial rows are kept for the audit trail
  and excluded from every `/performance` baseline — an outage leaves a gap in the history rather
  than a false percentage. (A Binance + Kraken outage once persisted a $2.64 total, which later
  became a 7d baseline and reported +442,768%.)
- **Rebalancing is refused outright.** A venue reporting `0.0` looks like its holdings were already
  sold, and trading on that sells the rest of the book down to match a portfolio that doesn't exist.

Absent credentials count as degraded: the bot holds assets on all four venues, so a missing client
means an incomplete read rather than an empty wallet. As a backstop for failures that report a
plausible-looking total, `SNAPSHOT_GATE_MIN_RATIO` marks any snapshot below that fraction of the
last clean one as partial; the gate disarms once that baseline is older than
`SNAPSHOT_GATE_MAX_AGE_HOURS`, so a genuine collapse cannot wedge writes forever.

## Architecture

```
run.py                          # Entry point, registers Telegram handlers
├── portfolio.py                # Portfolio state, rebalance logic, trade execution
├── data/
│   ├── balance.py              # Multi-source balance aggregation
│   ├── trading.py              # ccxt trade routing (Binance)
│   ├── database.py             # PostgreSQL signal/trade/snapshot storage
│   ├── prices.py               # CoinGecko price fetching
│   └── scraper.py              # Signal scraper (Playwright + TOTP)
├── utils/
│   ├── command_handlers.py     # Telegram command handlers
│   └── helpers.py              # JSON I/O, logging, formatting
├── summary.py                  # Message formatting
├── constants.py                # Environment variable loading
└── config/
    └── targets.json            # Target allocations
```

## Rebalancing

The bot computes the delta between each asset's current allocation and its target. Assets more than 3% off trigger a rebalance warning on `/check`.

`/rebalance` shows the exact trades needed. `/rebalance live` executes them as market orders:

All assets are traded on Binance using available Binance USDC. Balances on Kraken or
Arbitrum count toward the portfolio, but a leg that needs them is reported as sitting
on that venue rather than traded.

Sells execute before buys to free up USDC. Trade routing prefers direct pairs and falls back to routing through USDC.

Trades below the minimum are skipped as dust. The minimum is the larger of `MIN_TRADE_USD`
(default $1) and the exchange's own per-symbol NOTIONAL filter read from ccxt market metadata —
Binance rejects undersized orders with `-1013`, and a flat local floor can't predict that
(ETH/USDC sits near $5). Assets without an exchange pair are flagged and skipped.

### Manual-only assets

`MANUAL_ASSETS` (default `PAXG`) lists assets the portfolio tracks but no execution venue trades.
PAXG balances are read from Kraken, yet Binance rejects PAXG orders with
`-2010 "not permitted for this account"`, and the bot has no Kraken execution path — `krakenex` is
read-only. Their legs are reported as `✋ MANUAL` with the USD amount to trade by hand (PAXG on Kraken).
The sells funding them still run, so the USDC is waiting on Binance to be moved across.

## Signal Polling

The bot polls TRW for new RSPS signals every 10 minutes. Each signal's message timestamp is normalized (relative dates like "Today at 3:09 AM" become absolute `2026-04-18 03:09`) and stored in the database. Signals are deduplicated by timestamp to avoid reprocessing stale data. If TRW loads the page at an old scroll position, the scraper clicks the "Viewing older messages" banner to jump to the latest.

When a new signal is detected, targets are updated and the auto-rebalance only fires if any asset drifts more than 3% from the new targets. Otherwise the signal is applied silently and trades are skipped.

### Shorts in signals

Signals can call shorts. The parser reads each one as a negative weight, in any of these forms:
`10% SOL Short`, `10% SOL (SHORT)`, `10% Short SOL`, `Short: 10% SOL`, `-10% SOL`, or lines under a
`Shorts:` heading (up to the next `Longs:` heading). Signals are stored exactly as written, shorts
included, so a signal that only changes its shorts still counts as new.

A short is never a spot target: its weight is added to the USDC target, and the coin it names gets a
0% target, so any of it already held is sold rather than kept as a long. Telegram, Discord, and
`/info` list shorts separately and say where they are held.

The parser never resolves a doubtful line toward a long, because reading a short as a long buys the
coin the signal is betting against. When it can't tell which side an allocation is on (e.g.
`80% BTC, 10% SOL short` on one line), when one coin is listed as both long and short, or when a
short names no coin, the whole signal is rejected and the targets stay as they were. The failure
counts as a failed scrape (alerted on the third in a row) and shows in `/fetch_signal`.

The 95–105% total check uses the gross total (longs + shorts), so a short's weight is never spread
across the longs.

With `SHORTS_ENABLED` off, that USDC just sits wherever it is. With it on, every rebalance (manual or
after a new signal) brings the shorts on Hyperliquid perps to the latest stored signal:

- Each short is sized as its % of the whole portfolio (less the rebalance reserve), at the
  Hyperliquid mid. Only the difference from the open position is traded, as a market order capped
  1% through the mid. A coin the signal no longer shorts is covered in full with a reduce-only buy.
- The USDC target that holds the shorts' weight is their margin, so that USDC has to be on
  Hyperliquid. The bot never moves funds between venues: deposit it yourself. Shorts the free
  margin cannot carry are scaled down together, and the rest is reported with where the USDC sits.
- Positions are cross margin at `SHORT_LEVERAGE` (default 1x, where a coin has to roughly double
  before the account is at risk). Hyperliquid's account value, unrealized PnL included, counts as
  USDC in the portfolio total. A failed Hyperliquid read marks the snapshot partial and blocks the
  rebalance, like any other venue.
- Coins Hyperliquid lists per thousand units (SHIB as kSHIB) are found automatically. A coin with
  no perp is skipped and its weight stays in USDC.
- A new signal that only changes its shorts still triggers the auto-rebalance.
- Covers go first, so the margin they release can back the opens.
- A long perp is never covered as a short. With the book on Hyperliquid it is one of the bot's
  longs; otherwise it was opened by hand and is left alone.
- If the latest signal can't be read or Hyperliquid can't be reached, the positions are left as
  they are.

Use an API wallet, not the main key: create one at app.hyperliquid.xyz → More → API, approve it
for the main account, and set `HYPERLIQUID_PRIVATE_KEY` to its key. It can trade but not withdraw.
`HYPERLIQUID_ACCOUNT_ADDRESS` is the main account it trades for — the address you deposited to, not
the API wallet's own address.

### The book on Hyperliquid

`EXECUTION_VENUE=hyperliquid` moves the whole book there. Binance, Kraken and Arbitrum are no longer
read or traded, and their credentials can be dropped; only the Hyperliquid account counts, so move
everything before switching or the rest drops out of the total. Set `SHORTS_ENABLED=true` as well to
trade the shorts.

- Longs in BTC, ETH, SOL and HYPE are bought on spot (UBTC, UETH and USOL are Unit-bridged tokens).
  Every other coin has no spot book there, or one too thin to trade, and is held as a 1x cross perp
  long, which pays funding. PAXG is a perp too, so it is no longer manual-only.
- Holdings are the spot tokens plus the long perps, valued at Hyperliquid mids; CoinGecko is not
  used. A long perp's notional comes out of the USDC, so the coins and USDC add up to the equity.
- Each rebalance places every sell and cover first, then the buys and opens, scaled down together
  to the free USDC.
- A signal token outside the static list is tracked when Hyperliquid lists a perp for it.
- The account must be in Unified mode (Hyperliquid → Settings), where spot and perps share one USDC
  balance. In any other mode the rebalance trades nothing and says so: an API wallet cannot move
  USDC between the two. Portfolio margin is refused outright.
