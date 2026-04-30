import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append(text)


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


@pytest.fixture
def fake_context():
    return FakeContext()


@pytest.fixture(autouse=True)
def reset_command_handler_state(monkeypatch):
    """Reset module-level state in utils.command_handlers before each test."""
    from utils import command_handlers as ch

    monkeypatch.setattr(ch, "_credentials_invalid", False)
    monkeypatch.setattr(ch, "_rate_limit_until", None)
    monkeypatch.setattr(ch, "_scrape_failure_count", 0)
    monkeypatch.setattr(ch, "_poll_failure_count", 0)
    monkeypatch.setattr(ch, "_poll_success_count", 0)
    monkeypatch.setattr(ch, "_last_poll_time", None)
    monkeypatch.setattr(ch, "_last_poll_status", "")
    monkeypatch.setattr(ch, "CHAT_ID", "123")
