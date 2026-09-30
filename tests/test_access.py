"""Tests for which chats and users the bot answers."""
from datetime import datetime, timezone

from telegram import Chat, Message, Update, User

from utils.access import build_command_filter

GROUP = -1001234567890
OWNER = 111
STRANGER = 222


def _command(chat_id: int, chat_type: str, user_id: int) -> Update:
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, first_name="x", is_bot=False),
        text="/check",
    )
    return Update(update_id=1, message=message)


def _answers(command_filter, update: Update) -> bool:
    return bool(command_filter.check_update(update))


def test_without_an_allow_list_anyone_in_the_chat_is_answered():
    f = build_command_filter(str(GROUP), frozenset())

    assert _answers(f, _command(GROUP, Chat.SUPERGROUP, STRANGER))
    assert not _answers(f, _command(STRANGER, Chat.PRIVATE, STRANGER))
    assert not _answers(f, _command(-100999, Chat.SUPERGROUP, OWNER))


def test_allow_list_limits_the_group_to_listed_users():
    f = build_command_filter(str(GROUP), frozenset({OWNER}))

    assert _answers(f, _command(GROUP, Chat.SUPERGROUP, OWNER))
    assert not _answers(f, _command(GROUP, Chat.SUPERGROUP, STRANGER))


def test_allow_listed_users_can_use_a_private_chat():
    f = build_command_filter(str(GROUP), frozenset({OWNER}))

    assert _answers(f, _command(OWNER, Chat.PRIVATE, OWNER))
    assert not _answers(f, _command(STRANGER, Chat.PRIVATE, STRANGER))


def test_allow_listed_users_are_ignored_in_other_groups():
    f = build_command_filter(str(GROUP), frozenset({OWNER}))

    assert not _answers(f, _command(-100999, Chat.SUPERGROUP, OWNER))


def test_allow_list_works_without_a_chat_id():
    f = build_command_filter(None, frozenset({OWNER}))

    assert _answers(f, _command(OWNER, Chat.PRIVATE, OWNER))
    assert not _answers(f, _command(GROUP, Chat.SUPERGROUP, OWNER))


def test_with_nothing_configured_nobody_is_answered():
    f = build_command_filter(None, frozenset())

    assert not _answers(f, _command(OWNER, Chat.PRIVATE, OWNER))
