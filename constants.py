from os import getenv
from dotenv import load_dotenv


load_dotenv()
BINANCE_API_KEY = getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = getenv("BINANCE_API_SECRET")
KRAKEN_API_KEY = getenv("KRAKEN_API_KEY")
KRAKEN_API_SECRET = getenv("KRAKEN_API_SECRET")
BOT_TOKEN = getenv("BOT_TOKEN", "")
CHAT_ID = getenv("CHAT_ID")
DATABASE_URL = getenv("DATABASE_URL", "")
META_MASK = getenv("META_MASK", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CRYPTO_PRICES_URL = "https://api.coingecko.com/api/v3/simple/price"
# CoinGecko's free /simple/price endpoint only accepts CoinGecko coin ids
# (the `symbols` query param is a paid-plan feature), so map tracked symbols here.
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "SUI": "sui",
    "USDC": "usd-coin",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "LINK": "chainlink",
    "BNB": "binancecoin",
    "PAXG": "pax-gold",
    "HYPE": "hyperliquid",
}
MIN_TRADE_USD = float(getenv("MIN_TRADE_USD", "1.0"))
# Assets tracked in the portfolio that none of the bot's execution venues can trade.
# PAXG balances are read from Kraken (see data/balance.py) but Binance rejects PAXG
# orders with -2010 "not permitted for this account", so its legs are reported for
# manual execution instead of being sent to an exchange that will refuse them.
MANUAL_ASSETS = {
    asset.strip().upper()
    for asset in getenv("MANUAL_ASSETS", "PAXG").split(",")
    if asset.strip()
}
REBALANCE_RESERVE_PCT = float(getenv("REBALANCE_RESERVE_PCT", "0.5"))
# Backstop for the degraded-venue check in Balance.degraded: a snapshot worth
# less than this fraction of the last clean one is treated as a failed read
# rather than a real drawdown. Must be between 0 and 1 — a ratio of 1 or more
# would reject every write, including gains.
SNAPSHOT_GATE_MIN_RATIO = min(max(float(getenv("SNAPSHOT_GATE_MIN_RATIO", "0.2")), 0.0), 0.99)
# Only apply that gate against a recent baseline, so a genuine collapse cannot
# lock snapshots out forever: once the last clean row ages past this, writes resume.
SNAPSHOT_GATE_MAX_AGE_HOURS = float(getenv("SNAPSHOT_GATE_MAX_AGE_HOURS", "48"))
TRW_EMAIL = getenv("TRW_EMAIL")
TRW_PASSWORD = getenv("TRW_PASSWORD")
TRW_TOTP_SECRET = getenv("TRW_TOTP_SECRET")
HYPERLIQUID_PRIVATE_KEY = getenv("HYPERLIQUID_PRIVATE_KEY")
HYPERLIQUID_ACCOUNT_ADDRESS = getenv("HYPERLIQUID_ACCOUNT_ADDRESS")
TRW_SIGNAL_URL = "https://app.jointherealworld.com/chat/01GGDHGV32QWPG7FJ3N39K4FME/01H83QAX979K9R7QTMH74ATR8C"
