"""Unit tests for the login banner-detection branches in data/scraper.py._login."""
import os
import pytest

from data.scraper import _login, TRWInvalidCredentialsError, TRWRateLimitError


class FakeFirst:
    def __init__(self, *, visible=False, text=""):
        self._visible = visible
        self._text = text

    async def wait_for(self, timeout=None): pass
    async def click(self, force=False, timeout=None): pass
    async def fill(self, value): pass
    async def is_visible(self): return self._visible
    async def inner_text(self): return self._text


class FakeLocator:
    def __init__(self, *, count=1, visible=False, text=""):
        self._count = count
        self.first = FakeFirst(visible=visible, text=text)

    async def count(self): return self._count


class FakePage:
    """Scripts responses by selector/text so tests can steer _login to a specific branch."""

    def __init__(self, *, rate_limit_visible=False, rate_limit_text="",
                 invalid_creds_visible=False, invalid_creds_text=""):
        self.rate_limit_visible = rate_limit_visible
        self.rate_limit_text = rate_limit_text
        self.invalid_creds_visible = invalid_creds_visible
        self.invalid_creds_text = invalid_creds_text
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
