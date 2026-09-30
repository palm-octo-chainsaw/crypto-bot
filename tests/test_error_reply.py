"""Tests for the error details sent to the chat."""
from utils import command_handlers as ch


def _raised(error):
    try:
        raise error
    except Exception as caught:
        return caught


def test_error_reply_includes_the_traceback():
    reply = ch.format_error_reply("⚠️ Headline", _raised(RuntimeError("binance 503")))

    assert reply.startswith("⚠️ Headline\n\n<pre>")
    assert reply.endswith("</pre>")
    assert "Traceback" in reply
    assert "RuntimeError: binance 503" in reply


def test_error_reply_escapes_html():
    reply = ch.format_error_reply("a < b", _raised(ValueError("<b>&</b>")))

    assert reply.startswith("a &lt; b")
    assert "&lt;b&gt;&amp;&lt;/b&gt;" in reply


def test_error_reply_redacts_secrets(monkeypatch):
    monkeypatch.setattr(ch.constants, "BINANCE_API_SECRET", "s3cr3t-value")
    monkeypatch.setattr(ch.constants, "DATABASE_URL", "postgresql://bot:dbpass@db:5432/bot")

    reply = ch.format_error_reply(
        "x", _raised(RuntimeError("key s3cr3t-value, auth for bot:dbpass@db failed")),
    )

    assert "s3cr3t-value" not in reply
    assert "dbpass" not in reply
    assert ch.REDACTED in reply


def test_error_reply_fits_telegram_limit_and_keeps_the_tail():
    reply = ch.format_error_reply("x", _raised(ValueError("&" * 10_000 + "END")))

    assert len(reply) <= 4096
    assert "END" in reply
