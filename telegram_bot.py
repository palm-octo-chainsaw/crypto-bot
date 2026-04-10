import requests
import logging
from requests import RequestException, Response

from constants import TELEGRAM_API, CHAT_ID


logger = logging.getLogger(__name__)


class Bot():
    def __init__(self):
        self.url = TELEGRAM_API
        self.chat_id = CHAT_ID

    def send_message(self, message: str) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }

        try:
            response: Response = requests.post(f"{self.url}/sendMessage", json=payload, timeout=15)
            response.raise_for_status()
            logger.info("[Telegram] Notification sent successfully: %s", response)

        except RequestException as error:
            logger.error("[Telegram] Failed to send notification: %s", error)
