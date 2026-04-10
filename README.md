# crypto-telegram-bot

A Telegram bot for tracking a multi-source crypto portfolio and executing rebalancing trades on Binance.

## Features

- **Multi-source balance aggregation** — combines on-chain wallets, Binance, Kraken, and Hyperliquid into a single portfolio view
- **Portfolio rebalancing** — computes per-asset deltas against target allocations and executes market orders on Binance
- **Dry-run mode** — preview trades before executing anything real
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
| HYPE | Hyperliquid spot |

**Leverage tokens** (Arbitrum): `BTCBULL2X`, `BTCBULL4X`, `ETHBULL4X`

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file:

```env
# Telegram
BOT_TOKEN=your_telegram_bot_token
CHAT_ID=your_chat_id

# EVM wallet (Arbitrum token balances + Hyperliquid spot)
META_MASK=your_evm_wallet_address

# Binance (balance tracking + trade execution)
BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret

# Kraken (balance tracking only)
KRAKEN_API_KEY=your_kraken_api_key
KRAKEN_API_SECRET=your_kraken_api_secret
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

## Architecture

```
run.py                          # Entry point, registers Telegram handlers
├── portfolio.py                # Portfolio state, rebalance logic, trade execution
├── data/
│   ├── balance.py              # Multi-source balance aggregation
│   ├── trading.py              # ccxt trade routing and order execution
│   └── prices.py               # CoinGecko price fetching
├── utils/
│   ├── command_handlers.py     # Telegram command handlers
│   └── helpers.py              # JSON I/O, logging, formatting
├── summary.py                  # Message formatting
├── telegram_bot.py             # Telegram bot wrapper
├── constants.py                # Environment variable loading
└── config/
    └── targets.json            # Target allocations
```

## Rebalancing

The bot computes the delta between each asset's current allocation and its target. Assets more than 3% off trigger a rebalance warning on `/check`.

`/rebalance` shows the exact trades needed. `/rebalance live` executes them as market orders on Binance, selling over-allocated assets first (to free USDC), then buying under-allocated ones. Trade routing prefers direct pairs and falls back to routing through USDC.

Only assets on Binance can be traded. On-chain and Kraken/Hyperliquid balances are included in the portfolio calculation but must be rebalanced manually.
