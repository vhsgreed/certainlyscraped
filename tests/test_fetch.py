"""Tier 3 fetcher tests with fake browser backends (no browser, no network).
Run: python3 -m pytest tests/ -q"""
import sys
import types

sys.path.insert(0, ".")

import pytest

from certainlyscraped import fetch as fetch_mod


class _Resp:
    def __init__(self, status):
        self.status = status


@pytest.fixture
def no_backends(monkeypatch):
    # A None entry in sys.modules makes the import raise ImportError.
    for name in ("scrapling", "scrapling.fetchers", "cloakbrowser",
                 "playwright", "playwright.sync_api"):
        monkeypatch.setitem(sys.modules, name, None)
    return monkeypatch


def test_scrapling_gets_proxy_cookies_timeout_and_real_status(no_backends):
    seen = {}

    class StealthySession:
        def __init__(self, **kw):
            seen["init"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch(self, url, **kw):
            seen["url"] = url
            return types.SimpleNamespace(status=403, html_content="<html>wall</html>")

    no_backends.setitem(sys.modules, "scrapling.fetchers",
                        types.SimpleNamespace(StealthySession=StealthySession))
    status, html = fetch_mod.fetch("https://s/x", 3, timeout=12,
                                   proxy="http://p:8080", cookies={"sid": "1"})
    assert (status, html) == (403, "<html>wall</html>")
    assert seen["init"]["timeout"] == 12000
    assert seen["init"]["proxy"] == "http://p:8080"
    assert seen["init"]["cookies"] == [{"name": "sid", "value": "1", "url": "https://s/x"}]


def test_playwright_fallback_gets_proxy_cookies_and_real_status(no_backends):
    seen = {"closed": False}

    class Page:
        def goto(self, url, timeout, wait_until):
            seen["goto_timeout"] = timeout
            return _Resp(503)

        def content(self):
            return "<html>busy</html>"

    class Ctx:
        def add_cookies(self, c):
            seen["cookies"] = c

        def new_page(self):
            return Page()

    class Browser:
        def new_context(self):
            return Ctx()

        def close(self):
            seen["closed"] = True

    class Chromium:
        def launch(self, **kw):
            seen["launch"] = kw
            return Browser()

    class PW:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    no_backends.setitem(sys.modules, "playwright.sync_api",
                        types.SimpleNamespace(sync_playwright=PW))
    status, html = fetch_mod.fetch("https://s/x", 3, timeout=5,
                                   proxy="http://u:pw@p.example:3128",
                                   cookies={"a": "b"})
    assert (status, html) == (503, "<html>busy</html>")
    assert seen["launch"]["proxy"] == {"server": "http://p.example:3128",
                                       "username": "u", "password": "pw"}
    assert seen["cookies"] == [{"name": "a", "value": "b", "url": "https://s/x"}]
    assert seen["goto_timeout"] == 5000
    assert seen["closed"]


def test_no_backend_raises_fetch_error(no_backends):
    with pytest.raises(fetch_mod.FetchError):
        fetch_mod.fetch("https://s/x", 3)


# --- fix round 3: transport failures are FetchError ----------------------

def test_connect_error_becomes_a_fetch_error():
    """A real network failure must not escape as a raw httpx exception:
    Ladder._call only knows FetchError, and the page would lose its story."""
    import httpx

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    with pytest.raises(fetch_mod.FetchError) as exc:
        fetch_mod.fetch("https://h.test/1", 1, transport=transport)
    assert "ConnectError" in str(exc.value)
    assert "connection refused" in str(exc.value)


def test_read_timeout_becomes_a_fetch_error():
    import httpx

    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(fetch_mod.FetchError) as exc:
        fetch_mod.fetch("https://h.test/1", 1,
                        transport=httpx.MockTransport(handler))
    assert "ReadTimeout" in str(exc.value)


def test_proxy_error_keeps_its_message_until_redaction():
    """The proxy URL travels in the message; scrubbing is the caller's job."""
    import httpx

    def handler(request):
        raise httpx.ProxyError("http://u:s3cret@10.0.0.1:8080 refused")

    with pytest.raises(fetch_mod.FetchError) as exc:
        fetch_mod.fetch("https://h.test/1", 1,
                        transport=httpx.MockTransport(handler))
    assert "ProxyError" in str(exc.value)
    assert "10.0.0.1" in str(exc.value)


def test_guarded_transport_error_becomes_a_fetch_error():
    import httpx

    def handler(request):
        raise httpx.ConnectError("connect failed", request=request)

    with pytest.raises(fetch_mod.FetchError) as exc:
        fetch_mod.fetch("https://h.test/1", 1, guard=lambda url: None,
                        transport=httpx.MockTransport(handler))
    assert "ConnectError" in str(exc.value)


def test_guard_errors_are_not_rewrapped():
    """PrivateAddressError is the guard's verdict, not a transport failure."""
    class PrivateAddressError(ValueError):
        pass

    def guard(url):
        raise PrivateAddressError("private address: 127.0.0.1")

    with pytest.raises(PrivateAddressError):
        fetch_mod.fetch("https://h.test/1", 1, guard=guard)


def test_curl_cffi_transport_error_becomes_a_fetch_error(monkeypatch):
    cr = pytest.importorskip("curl_cffi.requests")
    from curl_cffi.requests import exceptions as cr_exc

    def boom(*args, **kwargs):
        raise cr_exc.ProxyError("proxy refused")

    monkeypatch.setattr(cr, "get", boom)
    with pytest.raises(fetch_mod.FetchError) as exc:
        fetch_mod.fetch("https://h.test/1", 2)
    assert "ProxyError" in str(exc.value)


def test_tier3_browser_failure_becomes_a_fetch_error(no_backends):
    class StealthySession:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch(self, url, **kw):
            raise RuntimeError("browser closed")

    no_backends.setitem(sys.modules, "scrapling.fetchers",
                        types.SimpleNamespace(StealthySession=StealthySession))
    with pytest.raises(fetch_mod.FetchError) as exc:
        fetch_mod.fetch("https://h.test/1", 3)
    assert "RuntimeError" in str(exc.value)
