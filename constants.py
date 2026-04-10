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
