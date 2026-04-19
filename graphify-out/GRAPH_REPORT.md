# Graph Report - .  (2026-04-18)

## Corpus Check
- Corpus is ~5,669 words - fits in a single context window. You may not need a graph.

## Summary
- 129 nodes · 193 edges · 14 communities detected
- Extraction: 92% EXTRACTED · 8% INFERRED · 0% AMBIGUOUS · INFERRED: 16 edges (avg confidence: 0.66)
- Token cost: 0 input · 0 output

## God Nodes (most connected - your core abstractions)
1. `Portfolio` - 18 edges
2. `Balance` - 18 edges
3. `Summary` - 9 edges
4. `_reply()` - 9 edges
5. `get_connection()` - 8 edges
6. `execute_trade()` - 5 edges
7. `_extract_signal()` - 5 edges
8. `_open_channel()` - 5 edges
9. `fetch_signal()` - 4 edges
10. `poll_signal()` - 4 edges

## Surprising Connections (you probably didn't know these)
- `Scheduled job: check TRW for a new signal; if found, update targets and live-reb` --uses--> `Portfolio`  [INFERRED]
  utils/command_handlers.py → portfolio.py
- `Portfolio` --uses--> `Balance`  [INFERRED]
  portfolio.py → data/balance.py
- `Classify rebalance amounts into sells, buys, and dust trades.` --uses--> `Balance`  [INFERRED]
  portfolio.py → data/balance.py
- `Execute all trades on one side (sells or buys). Returns result dicts.` --uses--> `Balance`  [INFERRED]
  portfolio.py → data/balance.py
- `Execute HYPE trade on Hyperliquid. Returns result dicts.` --uses--> `Balance`  [INFERRED]
  portfolio.py → data/balance.py

## Hyperedges (group relationships)
- **Trade Execution Pipeline** — readme_portfolio_rebalancing, readme_hyperliquid_trading, readme_binance_trading, readme_dust_filtering, readme_sells_before_buys [EXTRACTED 0.90]
- **Signal Processing Pipeline** — readme_signal_scraping, readme_stale_signal_detection, readme_timestamp_normalization [EXTRACTED 0.90]

## Communities

### Community 0 - "Portfolio & Rebalancing"
Cohesion: 0.12
Nodes (8): _format_trade_line(), _is_directly_tradeable(), Portfolio, Classify rebalance amounts into sells, buys, and dust trades., Execute all trades on one side (sells or buys). Returns result dicts., Execute HYPE trade on Hyperliquid. Returns result dicts., _trade_status(), Summary

### Community 1 - "Telegram Commands"
Cohesion: 0.2
Nodes (15): _apply_allocations(), fetch_signal(), _format_signal_message(), get_leverage_balance(), get_spot_balance(), get_targets(), get_total(), poll_signal() (+7 more)

### Community 2 - "Signal Scraper"
Cohesion: 0.16
Nodes (17): _extract_signal(), _extract_timestamp(), fetch_signal(), _handle_device_limit(), _jump_to_latest(), _login(), _normalize_timestamp(), _open_channel() (+9 more)

### Community 3 - "Balance Aggregation"
Cohesion: 0.29
Nodes (1): Balance

### Community 4 - "Trade Execution"
Cohesion: 0.23
Nodes (9): apply_precision(), execute_trade(), _fetch_hyperliquid_fee(), find_direct_pair(), place_order(), Trade routing and execution via ccxt., Return (symbol, side) if a direct market exists, else None., Fetch fee info for a Hyperliquid order from the fills API. (+1 more)

### Community 5 - "Database Layer"
Cohesion: 0.36
Nodes (9): get_connection(), get_latest_allocations(), get_latest_message_timestamp(), get_latest_signal_id(), init_db(), SQLite database for tracking signals, portfolio snapshots, and trades., record_signal(), record_snapshot() (+1 more)

### Community 6 - "Core Config & Pricing"
Cohesion: 0.33
Nodes (0): 

### Community 7 - "Trading Docs"
Cohesion: 0.47
Nodes (6): Binance Trading, Dust Filtering, Hyperliquid HYPE Trading, Portfolio Rebalancing, Sells Before Buys Rationale, ccxt

### Community 8 - "Utility Helpers"
Cohesion: 0.4
Nodes (0): 

### Community 9 - "Signal Docs"
Cohesion: 0.4
Nodes (5): Signal Scraping (RSPS), Stale Signal Detection, Timestamp Normalization, Playwright, pyotp

### Community 10 - "Bot Notifications"
Cohesion: 1.0
Nodes (2): Shutdown Notifications, python-telegram-bot

### Community 11 - "On-Chain Balance"
Cohesion: 1.0
Nodes (2): Multi-Source Balance Aggregation, web3

### Community 12 - "Kraken Integration"
Cohesion: 1.0
Nodes (2): Kraken Balance Tracking, krakenex

### Community 13 - "Project Overview"
Cohesion: 1.0
Nodes (1): Crypto Telegram Bot

## Knowledge Gaps
- **25 isolated node(s):** `SQLite database for tracking signals, portfolio snapshots, and trades.`, `Trade routing and execution via ccxt.`, `Return (symbol, side) if a direct market exists, else None.`, `Fetch fee info for a Hyperliquid order from the fills API.`, `Execute sell_token -> buy_token. Prefers direct pairs, falls back via stable.` (+20 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Bot Notifications`** (2 nodes): `Shutdown Notifications`, `python-telegram-bot`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `On-Chain Balance`** (2 nodes): `Multi-Source Balance Aggregation`, `web3`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Kraken Integration`** (2 nodes): `Kraken Balance Tracking`, `krakenex`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Project Overview`** (1 nodes): `Crypto Telegram Bot`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Portfolio` connect `Portfolio & Rebalancing` to `Telegram Commands`, `Balance Aggregation`?**
  _High betweenness centrality (0.161) - this node is a cross-community bridge._
- **Why does `Balance` connect `Balance Aggregation` to `Portfolio & Rebalancing`, `Core Config & Pricing`?**
  _High betweenness centrality (0.137) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `Portfolio` (e.g. with `Summary` and `Balance`) actually correct?**
  _`Portfolio` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 4 inferred relationships involving `Balance` (e.g. with `Portfolio` and `Classify rebalance amounts into sells, buys, and dust trades.`) actually correct?**
  _`Balance` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Are the 4 inferred relationships involving `Summary` (e.g. with `Portfolio` and `Classify rebalance amounts into sells, buys, and dust trades.`) actually correct?**
  _`Summary` has 4 INFERRED edges - model-reasoned connections that need verification._
- **What connects `SQLite database for tracking signals, portfolio snapshots, and trades.`, `Trade routing and execution via ccxt.`, `Return (symbol, side) if a direct market exists, else None.` to the rest of the system?**
  _25 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Portfolio & Rebalancing` be split into smaller, more focused modules?**
  _Cohesion score 0.12 - nodes in this community are weakly interconnected._