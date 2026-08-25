"""Signal parsing, message extraction and channel/session handling in data/scraper."""
import os
from datetime import datetime, timedelta

import pytest

import data.scraper as scraper
from data.scraper import parse_signal, _normalize_timestamp


# --- fakes -----------------------------------------------------------------

class FakeHandle:
    """One resolved element: text, visibility and click/typing are all no-ops."""

    def __init__(self, text="", visible=True, on_click=None):
        self._text = text
        self._visible = visible
        self._on_click = on_click
        self.clicks = 0

    async def wait_for(self, timeout=None): pass

    async def click(self, force=False, timeout=None):
        self.clicks += 1
        if self._on_click is not None:
            self._on_click()

    async def fill(self, value): pass
    async def is_visible(self): return self._visible
    async def inner_text(self): return self._text


class FakeLocator:
    def __init__(self, handles=None, count=None, visible=True, text="", on_click=None):
        if handles is None:
            handles = [FakeHandle(text=text, visible=visible, on_click=on_click)
                       for _ in range(count if count is not None else 1)]
        self._handles = handles

    async def count(self):
        return len(self._handles)

    @property
    def first(self):
        return self._handles[0]

    @property
    def last(self):
        return self._handles[-1]

    def nth(self, index):
        return self._handles[index]


EMPTY = lambda: FakeLocator(handles=[])


class FakeMessage:
    """A chat message element: body span plus an optional timestamp span."""

    def __init__(self, body="", timestamp=None, body_raises=False):
        self._body = body
        self._timestamp = timestamp
        self._body_raises = body_raises

    def locator(self, selector):
        if "custom-break-words" in selector:
            if self._body_raises:
                raise_handle = FakeHandle()

                async def boom():
                    raise ValueError("Node is not an HTMLElement")
                raise_handle.inner_text = boom
                return FakeLocator(handles=[raise_handle])
            return FakeLocator(handles=[FakeHandle(text=self._body)])
        if "opacity-50" in selector:
            if self._timestamp is None:
                return FakeLocator(handles=[])
            return FakeLocator(handles=[FakeHandle(text=self._timestamp)])
        return FakeLocator(handles=[])

    async def inner_text(self):
        return self._body


class FakeChannelPage:
    """A loaded channel page for _extract_signal / _jump_to_latest / _open_channel."""

    def __init__(self, *, messages=None, body_text="", url=scraper.TRW_SIGNAL_URL,
                 texts=(), goto_raises=None):
        self.messages = list(messages or [])
        self.body_text = body_text
        self.url = url
        self.texts = dict(texts)          # visible get_by_text() labels -> count
        self.screenshots = []
        self.gotos = []
        self.waits = []
        self.clicked = []
        self._goto_raises = goto_raises

    def locator(self, selector):
        if "message" in selector:
            return FakeLocator(handles=self.messages)
        return FakeLocator(handles=[])

    def get_by_text(self, text, exact=False):
        count = self.texts.get(text, 0)
        return FakeLocator(handles=[
            FakeHandle(text=text, on_click=lambda label=text: self.clicked.append(label))
            for _ in range(count)
        ])

    async def inner_text(self, selector):
        return self.body_text

    async def goto(self, url, wait_until=None, timeout=None):
        if self._goto_raises is not None:
            raise self._goto_raises
        self.gotos.append(url)

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)

    async def screenshot(self, path=None, full_page=False):
        self.screenshots.append(path)


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False
        self.contexts = []

    async def new_context(self, **kwargs):
        context = FakeContext(self.page, kwargs)
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page, kwargs):
        self.page = page
        self.kwargs = kwargs
        self.saved_state_to = None

    async def new_page(self):
        return self.page

    async def storage_state(self, path=None):
        self.saved_state_to = path
        with open(path, "w") as file:
            file.write("{}")


class FakeChromium:
    def __init__(self, pages):
        self._pages = list(pages)
        self.browsers = []

    async def launch(self, headless=True):
        browser = FakeBrowser(self._pages.pop(0))
        self.browsers.append(browser)
        return browser


class FakePlaywright:
    def __init__(self, *pages):
        self.chromium = FakeChromium(pages)


SIGNAL = """RSPS Signal:
40% BTC
30% ETH
30% CASH
Executive Summary
ignored 99% BTC
"""


# --- parse_signal ----------------------------------------------------------

def test_parse_signal_maps_cash_to_usdc():
    assert parse_signal(SIGNAL) == {"BTC": 40.0, "ETH": 30.0, "USDC": 30.0}


def test_parse_signal_ignores_unknown_tickers():
    text = "RSPS Signal:\n50% BTC\n50% FAKECOIN\n50% ETH"
    assert parse_signal(text) == {"BTC": 50.0, "ETH": 50.0}


def test_parse_signal_aggregates_duplicate_symbols():
    """Cash and stables on separate lines both land on USDC and must add up."""
    text = "RSPS Signal:\n60% BTC\n20% CASH\n20% STABLES"
    assert parse_signal(text) == {"BTC": 60.0, "USDC": 40.0}


def test_parse_signal_returns_empty_without_the_marker():
    assert parse_signal("no signal here, 50% BTC") == {}


def test_parse_signal_returns_empty_when_allocations_sum_to_zero():
    assert parse_signal("RSPS Signal:\n0% BTC\n0% ETH") == {}


def test_parse_signal_normalizes_a_total_outside_the_tolerance():
    """A signal that sums to 200% is rescaled rather than trusted as written."""
    result = parse_signal("RSPS Signal:\n100% BTC\n100% ETH")
    assert result == {"BTC": 50.0, "ETH": 50.0}


def test_parse_signal_keeps_a_total_inside_the_tolerance():
    result = parse_signal("RSPS Signal:\n50% BTC\n46% ETH")
    assert result == {"BTC": 50.0, "ETH": 46.0}


def test_parse_signal_reads_the_last_section_of_a_correction():
    """Corrections quote the original at the top — the live allocation is the last one."""
    text = ("RSPS Signal:\n90% BTC\n10% ETH\n"
            "———\nCorrection below\n"
            "RSPS Signal:\n60% BTC\n40% ETH")
    assert parse_signal(text) == {"BTC": 60.0, "ETH": 40.0}


# --- _normalize_timestamp --------------------------------------------------

def test_normalize_timestamp_today():
    today = datetime.now().strftime("%Y-%m-%d")
    assert _normalize_timestamp("Today at 3:09 AM") == f"{today} 03:09"


def test_normalize_timestamp_yesterday():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert _normalize_timestamp("Yesterday at 11:30 PM") == f"{yesterday} 23:30"


def test_normalize_timestamp_keeps_unparseable_time_of_day():
    today = datetime.now().strftime("%Y-%m-%d")
    assert _normalize_timestamp("Today at noon") == f"{today} noon"


def test_normalize_timestamp_absolute_date():
    assert _normalize_timestamp("04/07/2026") == "2026-04-07"


def test_normalize_timestamp_passes_through_unknown_formats():
    assert _normalize_timestamp("last week") == "last week"


# --- message extraction ----------------------------------------------------

@pytest.mark.asyncio
async def test_extract_timestamp_normalizes_the_span_text():
    element = FakeMessage(body="hi", timestamp="04/07/2026")
    assert await scraper._extract_timestamp(element) == "2026-04-07"


@pytest.mark.asyncio
async def test_extract_timestamp_is_none_without_a_span():
    assert await scraper._extract_timestamp(FakeMessage(body="hi")) is None


@pytest.mark.asyncio
async def test_extract_timestamp_is_none_for_a_blank_span():
    assert await scraper._extract_timestamp(FakeMessage(body="hi", timestamp="   ")) is None


@pytest.mark.asyncio
async def test_message_body_text_falls_back_to_the_element():
    class NoBodySpan(FakeMessage):
        def locator(self, selector):
            return FakeLocator(handles=[])

    assert await scraper._message_body_text(NoBodySpan(body="whole element")) == "whole element"


@pytest.mark.asyncio
async def test_message_body_text_treats_non_html_nodes_as_empty():
    """SVG icons match the message selector; inner_text raises on them."""
    assert await scraper._message_body_text(FakeMessage(body_raises=True)) == ""


@pytest.mark.asyncio
async def test_extract_signal_reads_the_newest_matching_message():
    page = FakeChannelPage(messages=[
        FakeMessage(body="RSPS Signal:\n90% BTC\n10% ETH", timestamp="04/06/2026"),
        FakeMessage(body="chatter"),
        FakeMessage(body=SIGNAL, timestamp="04/07/2026"),
    ])

    allocations, signal_time = await scraper._extract_signal(page)

    assert allocations == {"BTC": 40.0, "ETH": 30.0, "USDC": 30.0}
    assert signal_time == "2026-04-07"


@pytest.mark.asyncio
async def test_extract_signal_falls_back_to_the_page_body():
    """When no message element matches, the raw body text still carries the signal."""
    page = FakeChannelPage(messages=[FakeMessage(body="chatter")],
                           body_text="header\n" + SIGNAL)

    allocations, signal_time = await scraper._extract_signal(page)

    assert allocations == {"BTC": 40.0, "ETH": 30.0, "USDC": 30.0}
    assert signal_time is None, "the body fallback cannot read a message timestamp"


@pytest.mark.asyncio
async def test_extract_signal_screenshots_when_nothing_matches():
    page = FakeChannelPage(messages=[FakeMessage(body="chatter")], body_text="nothing here")

    with pytest.raises(RuntimeError, match="Could not find RSPS signal"):
        await scraper._extract_signal(page)

    assert page.screenshots == [scraper.DEBUG_SCREENSHOT]


@pytest.mark.asyncio
async def test_extract_signal_raises_when_allocations_do_not_parse():
    page = FakeChannelPage(messages=[FakeMessage(body="RSPS Signal: postponed this week")])

    with pytest.raises(RuntimeError, match="could not parse allocations"):
        await scraper._extract_signal(page)


# --- device limit and banners ---------------------------------------------

@pytest.mark.asyncio
async def test_handle_device_limit_logs_out_old_sessions():
    page = FakeChannelPage(texts={"Device Limit Reached": 1})
    clicked = []

    class LogoutPage(FakeChannelPage):
        def locator(self, selector):
            if "Logout" in selector:
                return FakeLocator(handles=[FakeHandle(on_click=lambda: clicked.append("logout"))
                                            for _ in range(2)])
            if "lucide-x" in selector:
                return FakeLocator(handles=[FakeHandle(on_click=lambda: clicked.append("close"))])
            return FakeLocator(handles=[])

    page = LogoutPage(texts={"Device Limit Reached": 1})

    await scraper._handle_device_limit(page)

    # Five passes at the modal, then the close button.
    assert clicked.count("logout") == 5
    assert clicked[-1] == "close"


@pytest.mark.asyncio
async def test_handle_device_limit_stops_when_the_modal_is_gone():
    page = FakeChannelPage(texts={})

    await scraper._handle_device_limit(page)

    assert page.clicked == []


@pytest.mark.asyncio
async def test_handle_device_limit_stops_without_logout_buttons():
    """Modal present but no buttons to click — bail out instead of looping."""
    page = FakeChannelPage(texts={"Device Limit Reached": 1})

    await scraper._handle_device_limit(page)

    assert page.clicked == []


@pytest.mark.asyncio
async def test_handle_device_limit_tolerates_a_stuck_close_button():
    class StuckClosePage(FakeChannelPage):
        def locator(self, selector):
            if "lucide-x" in selector:
                handle = FakeHandle()

                async def boom(force=False, timeout=None):
                    raise RuntimeError("intercepted")
                handle.click = boom
                return FakeLocator(handles=[handle])
            return FakeLocator(handles=[])

    await scraper._handle_device_limit(StuckClosePage(texts={}))


@pytest.mark.asyncio
async def test_jump_to_latest_clicks_the_banner():
    page = FakeChannelPage(texts={"Viewing older messages": 1})

    await scraper._jump_to_latest(page)

    assert page.clicked == ["Viewing older messages"]


@pytest.mark.asyncio
async def test_jump_to_latest_does_nothing_without_the_banner():
    page = FakeChannelPage(texts={})

    await scraper._jump_to_latest(page)

    assert page.clicked == []


# --- _open_channel ---------------------------------------------------------

@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "SESSION_DIR", str(tmp_path / "session"))
    return tmp_path / "session"


def _write_session(session_dir):
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "state.json").write_text("{}")


@pytest.mark.asyncio
async def test_open_channel_reuses_a_live_session(monkeypatch, session_dir):
    _write_session(session_dir)
    monkeypatch.setattr(scraper, "_is_logged_out", _async_returning(False))

    async def no_login(page):
        raise AssertionError("a live session must not re-login")
    monkeypatch.setattr(scraper, "_login", no_login)

    page = FakeChannelPage()
    playwright = FakePlaywright(page)

    browser, context, opened = await scraper._open_channel(playwright)

    assert opened is page
    assert browser.closed is False
    assert context.kwargs["storage_state"] == os.path.join(
        os.path.abspath(str(session_dir)), "state.json")


@pytest.mark.asyncio
async def test_open_channel_relogs_in_when_the_session_expired(monkeypatch, session_dir):
    _write_session(session_dir)
    monkeypatch.setattr(scraper, "_is_logged_out", _async_returning(True))
    logins = []
    monkeypatch.setattr(scraper, "_login", _async_recording(logins))

    expired_page, fresh_page = FakeChannelPage(), FakeChannelPage()
    playwright = FakePlaywright(expired_page, fresh_page)

    browser, context, opened = await scraper._open_channel(playwright)

    assert playwright.chromium.browsers[0].closed is True, "expired session's browser is closed"
    assert opened is fresh_page
    assert len(logins) == 1
    assert context.saved_state_to is not None, "a fresh login saves the session"


@pytest.mark.asyncio
async def test_open_channel_clears_a_device_limit_on_the_saved_session(monkeypatch, session_dir):
    _write_session(session_dir)
    monkeypatch.setattr(scraper, "_is_logged_out", _async_returning(False))
    handled = []
    monkeypatch.setattr(scraper, "_handle_device_limit", _async_recording(handled))

    page = FakeChannelPage(texts={"Device Limit Reached": 1})

    await scraper._open_channel(FakePlaywright(page))

    assert len(handled) == 1
    assert page.gotos == [scraper.TRW_SIGNAL_URL, scraper.TRW_SIGNAL_URL], "channel reloaded"


@pytest.mark.asyncio
async def test_open_channel_clears_a_device_limit_after_a_fresh_login(monkeypatch, session_dir):
    monkeypatch.setattr(scraper, "_login", _async_recording([]))
    handled = []
    monkeypatch.setattr(scraper, "_handle_device_limit", _async_recording(handled))

    page = FakeChannelPage(texts={"Device Limit Reached": 1})

    await scraper._open_channel(FakePlaywright(page))

    assert len(handled) == 1


@pytest.mark.asyncio
async def test_open_channel_can_skip_saving_the_session(monkeypatch, session_dir):
    monkeypatch.setattr(scraper, "_login", _async_recording([]))
    page = FakeChannelPage()

    _, context, _ = await scraper._open_channel(FakePlaywright(page), save_session=False)

    assert context.saved_state_to is None
    assert not (session_dir / "state.json").exists()


# --- fetch_signal ----------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_signal_requires_credentials(monkeypatch):
    monkeypatch.setattr(scraper, "TRW_EMAIL", None)

    with pytest.raises(ValueError, match="TRW_EMAIL"):
        await scraper.fetch_signal()


@pytest.mark.asyncio
async def test_fetch_signal_returns_allocations_and_closes_the_browser(monkeypatch):
    monkeypatch.setattr(scraper, "TRW_EMAIL", "test@example.com")
    monkeypatch.setattr(scraper, "TRW_PASSWORD", "pw")
    monkeypatch.setattr(scraper, "TRW_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

    class FakeAsyncPlaywright:
        async def __aenter__(self): return object()
        async def __aexit__(self, *exc): return False

    monkeypatch.setattr(scraper, "async_playwright", FakeAsyncPlaywright)

    page = FakeChannelPage(messages=[FakeMessage(body=SIGNAL, timestamp="04/07/2026")])
    browser = FakeBrowser(page)

    async def opener(p):
        return browser, object(), page
    monkeypatch.setattr(scraper, "_open_channel", opener)

    allocations, signal_time = await scraper.fetch_signal()

    assert allocations == {"BTC": 40.0, "ETH": 30.0, "USDC": 30.0}
    assert signal_time == "2026-04-07"
    assert browser.closed is True


# --- helpers ---------------------------------------------------------------

def _async_returning(value):
    async def _call(*args, **kwargs):
        return value
    return _call


def _async_recording(sink):
    async def _call(*args, **kwargs):
        sink.append(args)
    return _call
