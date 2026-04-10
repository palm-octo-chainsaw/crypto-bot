from os import getenv
from dotenv import load_dotenv


load_dotenv()
BINANCE_API_KEY = getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = getenv("BINANCE_API_SECRET")
KRAKEN_API_KEY = getenv("KRAKEN_API_KEY")
KRAKEN_API_SECRET = getenv("KRAKEN_API_SECRET")
BOT_TOKEN = getenv("BOT_TOKEN")
CHAT_ID = getenv("CHAT_ID")
META_MASK = getenv("META_MASK")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CRYPTO_PRICES_URL = "https://api.coingecko.com/api/v3/simple/price"
TRW_EMAIL = getenv("TRW_EMAIL")
TRW_PASSWORD = getenv("TRW_PASSWORD")
TRW_TOTP_SECRET = getenv("TRW_TOTP_SECRET")
TRW_SIGNAL_URL = "https://app.jointherealworld.com/chat/01GGDHGV32QWPG7FJ3N39K4FME/01H83QAX979K9R7QTMH74ATR8C"
