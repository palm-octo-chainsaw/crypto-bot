"""Shorts in a signal: read as negative weights, never bought, their weight held in USDC."""
import pytest

import constants
from data.scraper import AmbiguousSignalError, parse_signal
from utils import command_handlers as ch
from utils import discord
from utils.helpers import split_shorts
from tests.test_signal_flow import _scraper_raising, _scraper_returning, stub_portfolio  # noqa: F401


LONGS = {"BTC": 50.0, "ETH": 30.0, "USDC": 10.0}


# --- parse_signal ----------------------------------------------------------

@pytest.mark.parametrize("short_line", [
    "10% SOL Short",
    "10% SOL (SHORT)",
    "10% Short SOL",
    "10% SHORT SOL",
    "Short: 10% SOL",
    "-10% SOL",
    "−10% SOL",
])
def test_parse_signal_reads_each_short_format_as_negative(short_line):
    """Every one of these was read as a 10% long before shorts were recognised."""
    text = f"RSPS Signal:\n50% BTC\n30% ETH\n{short_line}\n10% CASH"
    assert parse_signal(text) == {**LONGS, "SOL": -10.0}


def test_parse_signal_reads_a_shorts_heading_until_the_next_heading():
    text = "RSPS Signal:\nShorts:\n10% SOL\n5% ETH\nLongs:\n60% BTC\n25% CASH"
    assert parse_signal(text) == {"BTC": 60.0, "USDC": 25.0, "SOL": -10.0, "ETH": -5.0}


def test_parse_signal_applies_an_opening_short_label_to_the_whole_line():
    text = "RSPS Signal:\n60% BTC\n30% CASH\nShorts: 5% SOL, 5% ETH"
    assert parse_signal(text) == {"BTC": 60.0, "USDC": 30.0, "SOL": -5.0, "ETH": -5.0}


def test_parse_signal_binds_a_short_word_between_percent_and_coin_to_that_coin():
    text = "RSPS Signal:\n80% BTC, 10% SHORT SOL, 10% CASH"
    assert parse_signal(text) == {"BTC": 80.0, "USDC": 10.0, "SOL": -10.0}


def test_parse_signal_refuses_a_line_it_cannot_split_into_sides():
    """'10% SOL short' among other allocations could qualify SOL or the whole line."""
    with pytest.raises(AmbiguousSignalError):
        parse_signal("RSPS Signal:\n80% BTC, 10% SOL short, 10% CASH")


def test_parse_signal_refuses_a_line_mixing_long_and_short_labels():
    with pytest.raises(AmbiguousSignalError):
        parse_signal("RSPS Signal:\nLong 90% BTC, short 10% SOL")


def test_parse_signal_refuses_a_coin_on_both_sides():
    with pytest.raises(AmbiguousSignalError, match="SOL"):
        parse_signal("RSPS Signal:\n80% BTC\n10% SOL\n10% SOL short")


def test_parse_signal_refuses_a_short_with_no_coin():
    """Dropping it would scale the longs up into the room the short takes."""
    with pytest.raises(AmbiguousSignalError):
        parse_signal("RSPS Signal:\n90% BTC\n10% SHORT")


def test_parse_signal_keeps_a_shorted_stable_as_cash():
    assert parse_signal("RSPS Signal:\nShorts:\n10% SOL\n90% CASH") == {"USDC": 90.0, "SOL": -10.0}


def test_parse_signal_reads_a_bullet_dash_as_a_long():
    assert parse_signal("RSPS Signal:\n- 60% BTC\n- 40% ETH") == {"BTC": 60.0, "ETH": 40.0}


def test_parse_signal_does_not_read_short_term_as_a_short():
    assert parse_signal("RSPS Signal:\n60% BTC (short-term)\n40% ETH") == {"BTC": 60.0, "ETH": 40.0}


def test_parse_signal_normalizes_on_the_gross_total():
    """A short counts toward the book's size; netting it would inflate the longs."""
    result = parse_signal("RSPS Signal:\n60% BTC\n40% ETH\n10% SOL short")
    assert result == pytest.approx({"BTC": 600 / 11, "ETH": 400 / 11, "SOL": -100 / 11})


def test_parse_signal_keeps_a_gross_total_inside_the_tolerance():
    assert parse_signal("RSPS Signal:\n90% BTC\n10% SOL short") == {"BTC": 90.0, "SOL": -10.0}


def test_split_shorts_returns_positive_weights():
    assert split_shorts({"BTC": 90.0, "SOL": -10.0}) == ({"BTC": 90.0}, {"SOL": 10.0})


# --- applying a signal -----------------------------------------------------

@pytest.fixture
def written(monkeypatch):
    out = {}
    monkeypatch.setattr(ch, "write_json", lambda path, data: out.update(data=dict(data)))
    return out


def test_apply_allocations_holds_shorts_in_usdc(stub_portfolio, written):
    stub_portfolio.targets = {"BTC": 50.0, "SOL": 20.0, "USDC": 30.0}

    ch._apply_allocations({"BTC": 70.0, "USDC": 20.0, "SOL": -10.0})

    assert stub_portfolio.targets == {"BTC": 70.0, "SOL": 0.0, "USDC": 30.0}
    assert written["data"] == stub_portfolio.targets


def test_apply_allocations_never_sets_a_negative_target(stub_portfolio, written):
    """A short listed before the cash line must not be overwritten by it."""
    stub_portfolio.targets = {"BTC": 100.0}

    ch._apply_allocations({"SOL": -10.0, "BTC": 80.0, "USDC": 10.0})

    assert stub_portfolio.targets == {"BTC": 80.0, "USDC": 20.0}
    assert all(pct >= 0 for pct in stub_portfolio.targets.values())


def test_apply_allocations_holds_shorts_even_with_the_flag_on(stub_portfolio, written, monkeypatch):
    """With shorts traded on Hyperliquid, their weight is still USDC: it is their margin."""
    monkeypatch.setattr(constants, "SHORTS_ENABLED", True)
    stub_portfolio.targets = {}

    ch._apply_allocations({"BTC": 90.0, "SOL": -10.0})

    assert stub_portfolio.targets == {"BTC": 90.0, "USDC": 10.0}


# --- reporting -------------------------------------------------------------

def test_signal_message_lists_shorts_apart_with_the_reason():
    message = ch._format_signal_message({"BTC": 90.0, "SOL": -10.0}, None)

    longs, shorts = message.split("🔻")
    assert "BTC: 90.0%" in longs
    assert "SOL" not in longs
    assert "Shorts, held in USDC:" in shorts
    assert "SOL: 10.0% short" in shorts
    assert "Total: 100.0%" in message


def test_signal_message_names_hyperliquid_when_the_flag_is_on(monkeypatch):
    monkeypatch.setattr(constants, "SHORTS_ENABLED", True)
    message = ch._format_signal_message({"BTC": 90.0, "SOL": -10.0}, None)
    assert "shorted on Hyperliquid, margined in USDC" in message
    assert "_" not in message


def test_signal_message_has_no_shorts_section_without_shorts():
    assert "🔻" not in ch._format_signal_message({"BTC": 100.0}, None)


def test_signal_message_is_markdown_safe():
    """The poller sends it as Markdown, where a stray underscore fails the send."""
    message = ch._format_signal_message({"BTC": 90.0, "SOL": -10.0}, None)
    assert "_" not in message


def test_signal_section_marks_shorts(monkeypatch):
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"BTC": 90.0, "SOL": -10.0})
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: None)
    out = "\n".join(ch._format_signal_section())
    assert "SOL: 10.0% short (held in USDC)" in out
    assert "-10.0" not in out


def test_discord_payload_labels_shorts_and_totals_gross():
    embed = discord.build_signal_payload({"BTC": 90.0, "SOL": -10.0}, None)["embeds"][0]
    assert [(f["name"], f["value"]) for f in embed["fields"]] == [
        ("BTC", "90.0%"), ("🔻 SOL short", "10.0%"),
    ]
    assert embed["footer"]["text"] == "Total: 100.0%"


# --- poll flow -------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_signal_rebalances_a_short_signal_on_long_targets_only(
        monkeypatch, fake_context, stub_portfolio, written):
    stub_portfolio.targets = {"BTC": 50.0, "SOL": 20.0, "USDC": 30.0}
    monkeypatch.setattr(ch, "scrape_signal",
                        _scraper_returning({"BTC": 70.0, "USDC": 20.0, "SOL": -10.0},
                                           "2026-11-06 10:00"))
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"BTC": 50.0, "SOL": 20.0, "USDC": 30.0})
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: "2026-11-05 10:00")
    recorded = {}
    monkeypatch.setattr(ch, "record_signal",
                        lambda allocs, message_timestamp=None: recorded.update(allocs=allocs))

    await ch.poll_signal(fake_context)

    assert recorded["allocs"]["SOL"] == -10.0  # the signal is stored as written
    assert written["data"] == {"BTC": 70.0, "SOL": 0.0, "USDC": 30.0}
    stub_portfolio.execute_rebalance.assert_called_once_with(dry_run=False)
    assert "SOL: 10.0% short" in fake_context.bot.sent[0]


@pytest.mark.asyncio
async def test_poll_signal_applies_nothing_from_an_ambiguous_signal(
        monkeypatch, fake_context, stub_portfolio, written):
    monkeypatch.setattr(ch, "scrape_signal", _scraper_raising(AmbiguousSignalError("which side?")))

    await ch.poll_signal(fake_context)

    assert written == {}
    stub_portfolio.execute_rebalance.assert_not_called()
    assert ch._last_poll_status == "scrape failed: AmbiguousSignalError"
