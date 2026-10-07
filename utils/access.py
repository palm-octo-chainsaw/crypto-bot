import logging

from telegram.ext import filters

logger = logging.getLogger(__name__)


def build_command_filter(chat_id: str | None, allowed_user_ids: frozenset[int]) -> filters.BaseFilter:
    """Decide who the bot answers.

    Commands trade real money and replies carry error traces, so the default is
    to ignore everyone: only the configured chat is answered, and with an
    allow-list only those users are, in that chat or in a private one.
    Updates that fail the filter are dropped silently, telling a stranger
    nothing about the bot.
    """
    if allowed_user_ids:
        places = filters.ChatType.PRIVATE
        if chat_id:
            places |= filters.Chat(chat_id=int(chat_id))
        return filters.User(user_id=allowed_user_ids) & places
    if chat_id:
        return filters.Chat(chat_id=int(chat_id))
    logger.error("Neither CHAT_ID nor ALLOWED_USER_IDS is set — ignoring every command")
    return ~filters.ALL
