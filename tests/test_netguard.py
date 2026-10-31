"""Private-address (SSRF) guard tests. No DNS and no network: every resolver
and every HTTP client is injected.

Run: python3 -m pytest tests/test_netguard.py -q
"""
import socket
import sys
from urllib.parse import urlsplit

sys.path.insert(0, ".")

import httpx
import pytest

from certainlyscraped import fetch as fetch_mod
from certainlyscraped.api import scrape
from certainlyscraped.classify import probe
from certainlyscraped.fetch import FetchError
from certainlyscraped.netguard import PrivateAddressError, check_url

PUBLIC_V4 = "93.184.215.14"


def _no_resolve(host, port, **kw):
    raise AssertionError(f"resolve() called for literal host {host!r}")


def _resolver(mapping):
    """Fake socket.getaddrinfo over {host: [address, ...]}."""
    calls = []

    def resolve(host, port, **kw):
        calls.append((host, port, kw))
        if host not in mapping:
            raise socket.gaierror(f"no such host: {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (addr, port)) for addr in mapping[host]]

    resolve.calls = calls
    return resolve


REFUSED = [
    "http://127.0.0.1/",
    "http://localhost./",
    "http://foo.localhost/",
    "http://0x7f.1/",
    "http://2130706433/",
    "http://0177.0.0.1/",
    "http://127.1/",
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://100.64.0.1/",          # CGNAT / Tailscale range
    "http://169.254.169.254/latest/",
    "http://[::1]/",
    "http://[fd00::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://0.0.0.0/",
]


@pytest.mark.parametrize("url", REFUSED)
def test_private_address_refused(url):
    # Literal addresses (including legacy IPv4 spellings) never need DNS.
    with pytest.raises(PrivateAddressError) as e:
        check_url(url, resolve=_no_resolve)
    msg = str(e.value)
    assert "private address" in msg
    assert urlsplit(url).hostname in msg


def test_scheme_refused():
    with pytest.raises(PrivateAddressError, match="scheme not allowed: ftp"):
        check_url("ftp://example.com/", resolve=_no_resolve)
    with pytest.raises(PrivateAddressError, match="scheme not allowed: file"):
        check_url("file:///etc/passwd", resolve=_no_resolve)


def test_credentials_never_leak_into_message():
    with pytest.raises(PrivateAddressError) as e:
        check_url("http://user:secret@127.0.0.1/", resolve=_no_resolve)
    msg = str(e.value)
    assert "127.0.0.1" in msg
    assert "secret" not in msg and "user" not in msg


def test_hostname_resolving_to_any_private_address_is_refused():
    resolve = _resolver({"mixed.example": [PUBLIC_V4, "10.0.0.5"]})
    with pytest.raises(PrivateAddressError, match="private address"):
        check_url("https://mixed.example/", resolve=resolve)
    assert resolve.calls[0][0] == "mixed.example"
    assert resolve.calls[0][1] == 443
    assert resolve.calls[0][2].get("proto") == socket.IPPROTO_TCP


def test_hostname_resolving_only_to_public_is_accepted():
    resolve = _resolver({"public.example": [PUBLIC_V4]})
    check_url("https://public.example/", resolve=resolve)


def test_literal_public_ip_is_accepted_without_dns():
    check_url(f"https://{PUBLIC_V4}/", resolve=_no_resolve)


def test_unresolvable_host_is_a_plain_value_error():
    def resolve(host, port, **kw):
        raise socket.gaierror("nope")

    with pytest.raises(ValueError) as e:
        check_url("http://nope.invalid/", resolve=resolve)
    assert type(e.value) is ValueError
    assert "cannot resolve nope.invalid" in str(e.value)


def test_allow_private_accepts_loopback_but_still_checks_scheme():
    check_url("http://127.0.0.1/", allow_private=True, resolve=_no_resolve)
    with pytest.raises(PrivateAddressError, match="scheme not allowed: ftp"):
        check_url("ftp://127.0.0.1/", allow_private=True, resolve=_no_resolve)


# --- fetch(): manual redirect following under a guard ----------------------

def _guard(resolve):
    def guard(url):
        check_url(url, resolve=resolve)
    return guard


def _mock(handler):
    return httpx.MockTransport(handler)


def test_redirect_into_private_refused():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    resolve = _resolver({"public.example": [PUBLIC_V4]})
    with pytest.raises(PrivateAddressError, match="private address"):
        fetch_mod.fetch("https://public.example/", 1,
                        guard=_guard(resolve),
                        transport=_mock(handler))
    # The private hop was checked before any request could be made to it.
    assert seen == ["https://public.example/"]


def test_relative_redirect_followed_and_checked():
    seen = []
    guarded = []

    def handler(request):
        seen.append(str(request.url))
        if str(request.url) == "https://public.example/a":
            return httpx.Response(302, headers={"Location": "/b"})
        return httpx.Response(200, text="ok")

    resolve = _resolver({"public.example": [PUBLIC_V4]})

    def guard(url):
        guarded.append(url)
        check_url(url, resolve=resolve)

    status, text = fetch_mod.fetch("https://public.example/a", 1, guard=guard,
                                   transport=_mock(handler))
    assert (status, text) == (200, "ok")
    assert seen == ["https://public.example/a", "https://public.example/b"]
    assert guarded == ["https://public.example/a", "https://public.example/b"]


def test_too_many_redirects():
    count = {"n": 0}

    def handler(request):
        count["n"] += 1
        return httpx.Response(302, headers={"Location": f"/hop{count['n']}"})

    guard = _guard(_resolver({"public.example": [PUBLIC_V4]}))
    with pytest.raises(FetchError, match="too many redirects"):
        fetch_mod.fetch("https://public.example/", 1, guard=guard,
                        transport=_mock(handler))
    assert count["n"] == 11  # 10 redirects followed, the 11th is refused


def test_tier3_disabled_under_guard():
    with pytest.raises(FetchError, match="tier 3 disabled under the MCP guard"):
        fetch_mod.fetch("https://public.example/", 3, guard=lambda u: None)


def test_file_url_refused_under_guard(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<html></html>")
    with pytest.raises(FetchError, match="file:// not allowed under the guard"):
        fetch_mod.fetch(page.as_uri(), 1, guard=lambda u: None)


def test_tier2_manual_redirect_refuses_private_hop(monkeypatch):
    from curl_cffi import requests as cr

    calls = []

    class Resp:
        def __init__(self, status, text="", headers=None):
            self.status_code = status
            self.text = text
            self.headers = headers or {}

    def fake_get(url, **kw):
        calls.append((url, kw))
        if url == "https://public.example/":
            return Resp(302, "", {"Location": "http://10.0.0.1/"})
        return Resp(200, "ok")

    monkeypatch.setattr(cr, "get", fake_get)
    resolve = _resolver({"public.example": [PUBLIC_V4]})
    with pytest.raises(PrivateAddressError, match="private address"):
        fetch_mod.fetch("https://public.example/", 2, guard=_guard(resolve))
    assert [u for u, _ in calls] == ["https://public.example/"]
    assert calls[0][1].get("allow_redirects") is False


def test_tier2_relative_redirect_followed(monkeypatch):
    from curl_cffi import requests as cr

    calls = []

    class Resp:
        def __init__(self, status, text="", headers=None):
            self.status_code = status
            self.text = text
            self.headers = headers or {}

    def fake_get(url, **kw):
        calls.append(url)
        if url == "https://public.example/a":
            return Resp(301, "", {"Location": "b"})
        return Resp(200, "done")

    monkeypatch.setattr(cr, "get", fake_get)
    guard = _guard(_resolver({"public.example": [PUBLIC_V4]}))
    assert fetch_mod.fetch("https://public.example/a", 2, guard=guard) == (200, "done")
    assert calls == ["https://public.example/a", "https://public.example/b"]


# --- classify.probe(), assist.load_page() and api.scrape() -----------------

def test_assist_load_page_applies_guard():
    from certainlyscraped.assist import load_page

    with pytest.raises(PrivateAddressError, match="private address"):
        load_page("http://127.0.0.1:9/", tier="1", log=None,
                  guard=lambda u: check_url(u))


def test_assist_load_page_refuses_file_under_guard(tmp_path):
    from certainlyscraped.assist import load_page

    page = tmp_path / "page.html"
    page.write_text("<html></html>")
    with pytest.raises(FetchError, match="file:// not allowed under the guard"):
        load_page(page.as_uri(), tier="1", log=None, guard=lambda u: None)


def test_scrape_auto_tier_passes_guard_to_probe(monkeypatch):
    from certainlyscraped import classify

    seen = {}

    def fake_probe(url, timeout=25, guard=None):
        seen["guard"] = guard
        raise PrivateAddressError(f"private address: {url}")

    monkeypatch.setattr(classify, "probe", fake_probe)
    cfg = {"url": "http://public.example/", "tier": "auto",
           "items": {"selector": "li"},
           "fields": {"t": {"selector": "li"}}}
    guard = lambda u: check_url(u)  # noqa: E731
    with pytest.raises(PrivateAddressError):
        scrape(cfg, guard=guard, log=None)
    assert seen["guard"] is guard


def test_probe_propagates_private_address_error():
    def guard(url):
        raise PrivateAddressError(f"private address: {url}")

    with pytest.raises(PrivateAddressError):
        probe("http://127.0.0.1/", guard=guard)


def test_scrape_default_fetcher_applies_guard():
    cfg = {
        "url": "http://127.0.0.1:9/",
        "items": {"selector": "li"},
        "fields": {"t": {"selector": "li"}},
        "tier": 1,
        "rate_limit_sec": 0,
    }
    with pytest.raises(PrivateAddressError, match="private address"):
        scrape(cfg, guard=lambda u: check_url(u), log=None)
