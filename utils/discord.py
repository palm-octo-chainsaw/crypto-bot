import asyncio
import logging
import time

import requests

import constants

logger = logging.getLogger(__name__)

POST_ATTEMPTS = 3
POST_TIMEOUT_SECONDS = 10
POST_RETRY_BASE_DELAY_SECONDS = 2.0
# Discord's 429 body carries retry_after; never sleep longer than this on it,
# so a misbehaving hint cannot stall the poll job.
MAX_RETRY_AFTER_SECONDS = 30.0
EMBED_COLOR = 0x2ECC71


def build_signal_payload(allocations: dict, signal_time: str | None) -> dict:
    """Webhook body for a new signal: one embed, one inline field per asset."""
    fields = [
        {"name": symbol, "value": f"{pct}%", "inline": True}
        for symbol, pct in allocations.items()
    ]
    embed = {
        "title": "🆕 New RSPS Signal",
        "color": EMBED_COLOR,
        "fields": fields,
        "footer": {"text": f"Total: {sum(allocations.values())}%"},
    }
    if signal_time:
        embed["description"] = f"🕐 Signal posted: {signal_time}"
    # No mentions at all: the payload is built from scraped text, which must
    # never be able to ping @everyone or a role in the channel.
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def _post(url: str, payload: dict) -> bool:
    for attempt in range(1, POST_ATTEMPTS + 1):
        try:
            response = requests.post(url, json=payload, timeout=POST_TIMEOUT_SECONDS)
        except requests.RequestException as err:
            # The exception text can include the full URL, which is the secret.
            logger.warning(
                "Discord post failed (attempt %d/%d): %s",
                attempt, POST_ATTEMPTS, type(err).__name__,
            )
            delay = POST_RETRY_BASE_DELAY_SECONDS * attempt
        else:
            if response.ok:
                return True
            if response.status_code == 429:
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except ValueError:
                    retry_after = 1.0
                delay = min(retry_after, MAX_RETRY_AFTER_SECONDS)
                logger.warning(
                    "Discord rate-limited (attempt %d/%d) — retrying in %.1fs",
                    attempt, POST_ATTEMPTS, delay,
                )
            elif response.status_code >= 500:
                delay = POST_RETRY_BASE_DELAY_SECONDS * attempt
                logger.warning(
                    "Discord post failed (attempt %d/%d): HTTP %d",
                    attempt, POST_ATTEMPTS, response.status_code,
                )
            else:
                # 4xx other than 429 means the webhook or payload is wrong;
                # resending it fails identically.
                logger.error("Discord rejected post: HTTP %d", response.status_code)
                return False
        if attempt < POST_ATTEMPTS:
            time.sleep(delay)
    logger.error("Discord post failed after %d attempts", POST_ATTEMPTS)
    return False


async def post_signal(allocations: dict, signal_time: str | None) -> bool | None:
    """Post a new signal to the Discord webhook.

    Returns None when no webhook is configured, otherwise whether the post
    landed. Never raises: a Discord outage must not block the rebalance.
    """
    url = constants.DISCORD_WEBHOOK_URL
    if not url:
        return None
    try:
        payload = build_signal_payload(allocations, signal_time)
        return await asyncio.to_thread(_post, url, payload)
    except Exception as err:
        logger.error("Discord post crashed: %s", type(err).__name__)
        return False
