"""Unit tests for the login branches in data/scraper.py._login and fetch_signal."""
import os
import pytest
from playwright.async_api import TimeoutError as PwTimeout

import data.scraper as scraper
from data.scraper import _login, TRWInvalidCredentialsError, TRWRateLimitError


class FakeFirst:
    def __init__(self, *, visible=False, text="", wait_for_exc=None):
        self._visible = visible
        self._text = text
        self._wait_for_exc = wait_for_exc

    async def wait_for(self, timeout=None):
        if self._wait_for_exc is not None:
            raise self._wait_for_exc
    async def click(self, force=False, timeout=None): pass
    async def fill(self, value): pass
    async def is_visible(self): return self._visible
    async def inner_text(self): return self._text


class FakeLocator:
    def __init__(self, *, count=1, visible=False, text="", wait_for_exc=None):
        self._count = count
        self.first = FakeFirst(visible=visible, text=text, wait_for_exc=wait_for_exc)

    async def count(self): return self._count


class FakePage:
    """Scripts responses by selector/text so tests can steer _login to a specific branch."""

    def __init__(self, *, rate_limit_visible=False, rate_limit_text="",
                 invalid_creds_visible=False, invalid_creds_text="",
                 email_form_missing=False):
        self.rate_limit_visible = rate_limit_visible
        self.rate_limit_text = rate_limit_text
        self.invalid_creds_visible = invalid_creds_visible
        self.invalid_creds_text = invalid_creds_text
        self.email_form_missing = email_form_missing
        self.screenshots = []
        self.keyboard = _FakeKeyboard()

    def get_by_text(self, text, exact=False):
        return FakeLocator(count=1)

    def locator(self, selector):
        if "Too many" in selector:
            return FakeLocator(
                count=1 if self.rate_limit_visible else 0,
                visible=self.rate_limit_visible,
                text=self.rate_limit_text,
            )
        if "Invalid credentials" in selector:
            return FakeLocator(
                count=1 if self.invalid_creds_visible else 0,
                visible=self.invalid_creds_visible,
                text=self.invalid_creds_text,
            )
        if self.email_form_missing and 'input[type="email"' in selector:
            # Simulate the login form never rendering: the email field wait times out.
            return FakeLocator(count=1, visible=True, wait_for_exc=PwTimeout("no email field"))
        # Any other locator (email, password, totp candidates) returns a generic visible hit.
        return FakeLocator(count=1, visible=True)

    async def wait_for_timeout(self, ms): pass
    async def wait_for_url(self, url, timeout=None): pass
    async def screenshot(self, path=None, full_page=False):
        self.screenshots.append(path)


class _FakeKeyboard:
    async def type(self, text, delay=0): pass


@pytest.fixture(autouse=True)
def stub_env(monkeypatch):
    monkeypatch.setattr("data.scraper.TRW_EMAIL", "test@example.com")
    monkeypatch.setattr("data.scraper.TRW_PASSWORD", "pw")
    monkeypatch.setattr("data.scraper.TRW_TOTP_SECRET", "JBSWY3DPEHPK3PXP")


@pytest.mark.asyncio
async def test_login_raises_invalid_credentials_on_banner():
    page = FakePage(
        invalid_creds_visible=True,
        invalid_creds_text="Invalid credentials. Check your input and try again.",
    )
    with pytest.raises(TRWInvalidCredentialsError) as exc:
        await _login(page)
    assert "Invalid credentials" in str(exc.value)
    assert page.screenshots, "should screenshot on invalid-creds path"


@pytest.mark.asyncio
async def test_login_raises_rate_limit_on_banner():
    page = FakePage(
        rate_limit_visible=True,
        rate_limit_text="Too many failed login attempts, please try again in 45 minutes.",
    )
    with pytest.raises(TRWRateLimitError) as exc:
        await _login(page)
    assert exc.value.retry_after_minutes == 45


@pytest.mark.asyncio
async def test_rate_limit_takes_precedence_over_invalid_creds():
    """Rate-limit check runs first; if both banners somehow appear, rate-limit wins."""
    page = FakePage(
        rate_limit_visible=True,
        rate_limit_text="Too many failed login attempts, please try again in 30 minutes.",
        invalid_creds_visible=True,
        invalid_creds_text="Invalid credentials.",
    )
    with pytest.raises(TRWRateLimitError):
        await _login(page)


@pytest.mark.asyncio
async def test_login_screenshots_when_form_does_not_appear():
    """When the email field never renders, _login captures a screenshot and re-raises."""
    page = FakePage(email_form_missing=True)
    with pytest.raises(PwTimeout):
        await _login(page)
    assert page.screenshots, "should screenshot when the login form fails to appear"


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeFetchPage:
    def __init__(self):
        self.screenshots = []

    async def wait_for_timeout(self, ms): pass
    async def screenshot(self, path=None, full_page=False):
        self.screenshots.append(path)


class _FakeAsyncPlaywright:
    async def __aenter__(self): return object()
    async def __aexit__(self, *exc): return False


def _fake_async_playwright():
    return _FakeAsyncPlaywright()


@pytest.mark.asyncio
async def test_fetch_signal_timeout_without_page_reports_no_screenshot(monkeypatch):
    """If the timeout happens before a page exists, fetch_signal re-raises without a screenshot."""
    monkeypatch.setattr(scraper, "async_playwright", _fake_async_playwright)

    async def boom(p):
        raise PwTimeout("form never loaded")

    monkeypatch.setattr(scraper, "_open_channel", boom)

    with pytest.raises(RuntimeError) as exc:
        await scraper.fetch_signal()
    assert "TRW page timed out" in str(exc.value)


@pytest.mark.asyncio
async def test_fetch_signal_timeout_with_page_saves_screenshot(monkeypatch):
    """A timeout after the page is ready saves a screenshot and closes the browser."""
    monkeypatch.setattr(scraper, "async_playwright", _fake_async_playwright)
    browser = _FakeBrowser()
    page = _FakeFetchPage()

    async def opener(p):
        return browser, object(), page

    async def jumper(pg):
        raise PwTimeout("jump failed")

    monkeypatch.setattr(scraper, "_open_channel", opener)
    monkeypatch.setattr(scraper, "_jump_to_latest", jumper)

    with pytest.raises(RuntimeError):
        await scraper.fetch_signal()
    assert page.screenshots, "should screenshot when a page is available at timeout"
    assert browser.closed, "browser should be closed in the finally block"


@pytest.mark.asyncio
async def test_login_tolerates_screenshot_failure():
    """A failing screenshot during a form-missing timeout must not mask the original error."""
    page = FakePage(email_form_missing=True)

    async def boom(path=None, full_page=False):
        raise RuntimeError("screenshot backend unavailable")

    page.screenshot = boom
    with pytest.raises(PwTimeout):
        await _login(page)


@pytest.mark.asyncio
async def test_fetch_signal_tolerates_screenshot_failure(monkeypatch):
    """fetch_signal still raises the timeout RuntimeError even if the screenshot save fails."""
    monkeypatch.setattr(scraper, "async_playwright", _fake_async_playwright)
    browser = _FakeBrowser()
    page = _FakeFetchPage()

    async def boom(path=None, full_page=False):
        raise RuntimeError("screenshot backend unavailable")

    page.screenshot = boom

    async def opener(p):
        return browser, object(), page

    async def jumper(pg):
        raise PwTimeout("jump failed")

    monkeypatch.setattr(scraper, "_open_channel", opener)
    monkeypatch.setattr(scraper, "_jump_to_latest", jumper)

    with pytest.raises(RuntimeError) as exc:
        await scraper.fetch_signal()
    assert "TRW page timed out" in str(exc.value)
    assert browser.closed
