"""Tests for _message_body_text — extracts reply body, ignoring quote preview."""
import pytest

from data.scraper import _message_body_text, parse_signal


class FakeFirst:
    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text


class FakeLocator:
    def __init__(self, count, text=""):
        self._count = count
        self.first = FakeFirst(text)

    async def count(self):
        return self._count


class FakeElement:
    def __init__(self, body_text=None, full_text=""):
        self._body_text = body_text
        self._full_text = full_text

    def locator(self, selector):
        if selector == "span.custom-break-words":
            if self._body_text is None:
                return FakeLocator(0)
            return FakeLocator(1, self._body_text)
        return FakeLocator(0)

    async def inner_text(self):
        return self._full_text


@pytest.mark.asyncio
async def test_body_text_uses_body_span_when_present():
    el = FakeElement(body_text="Been long since", full_text="QUOTE_PREVIEW Been long since")
    assert await _message_body_text(el) == "Been long since"


@pytest.mark.asyncio
async def test_body_text_falls_back_to_inner_text():
    el = FakeElement(body_text=None, full_text="full message text")
    assert await _message_body_text(el) == "full message text"


@pytest.mark.asyncio
async def test_body_text_returns_empty_for_non_html_node():
    """Non-HTML nodes (e.g. SVG icons) raise on inner_text; treat as empty so
    the extraction scan continues instead of aborting."""

    class NonHtmlElement(FakeElement):
        async def inner_text(self):
            raise Exception("Locator.inner_text: Error: Node is not an HTMLElement")

    el = NonHtmlElement(body_text=None)
    assert await _message_body_text(el) == ""


def test_parse_signal_reply_body_without_marker_yields_empty():
    """A reply whose body has no 'rsps signal' marker should not parse stale allocations."""
    reply_body = "Been long since"
    assert parse_signal(reply_body) == {}


def test_parse_signal_extracts_from_actual_signal_message():
    text = (
        "RSPS Signal:\n"
        "- 42.9% ETH\n"
        "- 57.1% Cash\n"
        "Executive Summary:\n"
    )
    allocations = parse_signal(text)
    assert allocations == pytest.approx({"ETH": 42.9, "USDC": 57.1})
