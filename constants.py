from os import getenv
from dotenv import load_dotenv


load_dotenv()
BINANCE_API_KEY = getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = getenv("BINANCE_API_SECRET")
KRAKEN_API_KEY = getenv("KRAKEN_API_KEY")
KRAKEN_API_SECRET = getenv("KRAKEN_API_SECRET")
BOT_TOKEN = getenv("BOT_TOKEN", "")
CHAT_ID = getenv("CHAT_ID")
# Telegram user ids allowed to run commands, comma-separated. Empty means anyone
# in CHAT_ID may; set it once CHAT_ID is a group whose members should not trade.
ALLOWED_USER_IDS = frozenset(
    int(uid) for uid in getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()
)
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
    "NEAR": "near",
}
MIN_TRADE_USD = float(getenv("MIN_TRADE_USD", "1.0"))
# Currency values are shown in until /currency picks another. The bot still counts
# in USD; only what reaches the chat is converted.
DISPLAY_CURRENCY = getenv("DISPLAY_CURRENCY", "EUR").strip().upper()
# ECB reference rates, no key needed.
FX_RATES_URL = "https://api.frankfurter.dev/v1/latest"
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
# Where the book is held and traded. "binance" reads Binance, Kraken and Arbitrum
# and trades spot on Binance. "hyperliquid" reads and trades only the Hyperliquid
# account: longs on spot where its book is deep, as a 1x perp elsewhere.
EXECUTION_VENUE = getenv("EXECUTION_VENUE", "binance").strip().lower()
if EXECUTION_VENUE not in {"binance", "hyperliquid"}:
    raise ValueError(f"EXECUTION_VENUE must be binance or hyperliquid, not {EXECUTION_VENUE!r}")


def on_hyperliquid() -> bool:
    """Whether the whole book lives on Hyperliquid. Read at call time, so tests can flip it."""
    return EXECUTION_VENUE == "hyperliquid"
# Opt-in to trading a signal's shorts as Hyperliquid perps, on either execution
# venue. Off, a short's weight is held in USDC. On, that USDC is the shorts'
# margin: it has to sit on Hyperliquid, and each short is opened against it at
# SHORT_LEVERAGE.
SHORTS_ENABLED = getenv("SHORTS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
# The main Hyperliquid account, and the private key of an API wallet approved for
# it (app.hyperliquid.xyz → More → API). The API wallet can trade but not withdraw.
HYPERLIQUID_ACCOUNT_ADDRESS = getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "")
HYPERLIQUID_PRIVATE_KEY = getenv("HYPERLIQUID_PRIVATE_KEY", "")
# Cross-margin leverage set on each shorted coin. At 1x the margin equals the
# position, so a coin has to roughly double before the account is at risk.
SHORT_LEVERAGE = max(int(getenv("SHORT_LEVERAGE", "1")), 1)
# Backstop for the degraded-venue check in Balance.degraded: a snapshot worth
# less than this fraction of the last clean one is treated as a failed read
# rather than a real drawdown. Must be between 0 and 1 — a ratio of 1 or more
# would reject every write, including gains.
SNAPSHOT_GATE_MIN_RATIO = min(max(float(getenv("SNAPSHOT_GATE_MIN_RATIO", "0.2")), 0.0), 0.99)
# Only apply that gate against a recent baseline, so a genuine collapse cannot
# lock snapshots out forever: once the last clean row ages past this, writes resume.
SNAPSHOT_GATE_MAX_AGE_HOURS = float(getenv("SNAPSHOT_GATE_MAX_AGE_HOURS", "48"))
# Optional: new signals are also posted here. Empty disables Discord.
DISCORD_WEBHOOK_URL = getenv("DISCORD_WEBHOOK_URL", "")
TRW_EMAIL = getenv("TRW_EMAIL")
TRW_PASSWORD = getenv("TRW_PASSWORD")
TRW_TOTP_SECRET = getenv("TRW_TOTP_SECRET")
TRW_SIGNAL_URL = "https://app.jointherealworld.com/chat/01GGDHGV32QWPG7FJ3N39K4FME/01H83QAX979K9R7QTMH74ATR8C"
