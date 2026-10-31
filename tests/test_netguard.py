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
    # R28: the links path records a page-level refusal as an error outcome
    # (it no longer lets the exception escape the API unredacted).
    res = scrape(cfg, guard=lambda u: check_url(u), log=None)
    assert res.rows == []
    assert [o.outcome for o in res.page_report] == ["error"]
    assert "private address" in res.page_report[0].note


# --- the proxy URL is guarded too (fix round 1, R12) -----------------------

PRIVATE_PROXY = "http://127.0.0.1:2375"
PUBLIC_PROXY = f"http://{PUBLIC_V4}:3128"


def test_tier1_private_proxy_refused_before_any_request():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="ok")

    resolve = _resolver({"public.example": [PUBLIC_V4]})
    with pytest.raises(PrivateAddressError, match="127.0.0.1") as e:
        fetch_mod.fetch("https://public.example/", 1, proxy=PRIVATE_PROXY,
                        guard=_guard(resolve), transport=_mock(handler))
    assert "2375" not in str(e.value)  # message names the host, not credentials
    assert seen == []                  # the transport never saw a request


class _FakeClient:
    """Stands in for httpx.Client so a proxy test never opens a socket."""

    def __init__(self, calls):
        self.calls = calls
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        self.calls.append(("client", kwargs.get("proxy")))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        self.calls.append(("get", url))
        return httpx.Response(200, text="ok")


def _fake_httpx_client(monkeypatch):
    import httpx as _httpx

    calls = []
    fake = _FakeClient(calls)
    monkeypatch.setattr(_httpx, "Client", fake)
    return calls


def test_tier1_public_proxy_allowed(monkeypatch):
    calls = _fake_httpx_client(monkeypatch)
    guarded = []

    def guard(url):
        guarded.append(url)
        check_url(url, resolve=_resolver({"public.example": [PUBLIC_V4]}))

    status, text = fetch_mod.fetch("https://public.example/", 1,
                                   proxy=PUBLIC_PROXY, guard=guard)
    assert (status, text) == (200, "ok")
    assert guarded == [PUBLIC_PROXY, "https://public.example/"]
    assert calls == [("client", PUBLIC_PROXY), ("get", "https://public.example/")]


def test_tier1_private_proxy_allowed_with_allow_private(monkeypatch):
    calls = _fake_httpx_client(monkeypatch)

    def guard(url):
        check_url(url, allow_private=True, resolve=_no_resolve)

    status, text = fetch_mod.fetch("https://public.example/", 1,
                                   proxy=PRIVATE_PROXY, guard=guard)
    assert (status, text) == (200, "ok")
    assert calls == [("client", PRIVATE_PROXY),
                     ("get", "https://public.example/")]


def test_tier1_proxy_credentials_never_leak():
    def handler(request):
        return httpx.Response(200, text="ok")

    with pytest.raises(PrivateAddressError) as e:
        fetch_mod.fetch("https://public.example/", 1,
                        proxy="http://user:secret@127.0.0.1:2375",
                        guard=_guard(_resolver({"public.example": [PUBLIC_V4]})),
                        transport=_mock(handler))
    assert "127.0.0.1" in str(e.value)
    assert "secret" not in str(e.value) and "user" not in str(e.value)


def _fake_curl_cffi(monkeypatch, calls):
    from curl_cffi import requests as cr

    class Resp:
        def __init__(self, status, text="", headers=None):
            self.status_code = status
            self.text = text
            self.headers = headers or {}

    def fake_get(url, **kw):
        calls.append((url, kw))
        return Resp(200, "ok")

    monkeypatch.setattr(cr, "get", fake_get)


def test_tier2_private_proxy_refused_before_any_request(monkeypatch):
    calls = []
    _fake_curl_cffi(monkeypatch, calls)
    resolve = _resolver({"public.example": [PUBLIC_V4]})
    with pytest.raises(PrivateAddressError, match="127.0.0.1"):
        fetch_mod.fetch("https://public.example/", 2, proxy=PRIVATE_PROXY,
                        guard=_guard(resolve))
    assert calls == []


def test_tier2_public_proxy_allowed(monkeypatch):
    calls = []
    _fake_curl_cffi(monkeypatch, calls)
    resolve = _resolver({"public.example": [PUBLIC_V4]})
    assert fetch_mod.fetch("https://public.example/", 2, proxy=PUBLIC_PROXY,
                           guard=_guard(resolve)) == (200, "ok")
    assert [u for u, _ in calls] == ["https://public.example/"]
    assert calls[0][1].get("proxy") == PUBLIC_PROXY


def test_tier2_private_proxy_allowed_with_allow_private(monkeypatch):
    calls = []
    _fake_curl_cffi(monkeypatch, calls)

    def guard(url):
        check_url(url, allow_private=True, resolve=_no_resolve)

    assert fetch_mod.fetch("https://public.example/", 2, proxy=PRIVATE_PROXY,
                           guard=guard) == (200, "ok")
    assert [u for u, _ in calls] == ["https://public.example/"]


def test_scrape_guards_the_config_proxy(monkeypatch):
    from certainlyscraped import api as api_mod

    real_fetch = fetch_mod.fetch
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="<html><li>x</li></html>")

    def fake_fetch(url, tier, **kw):
        return real_fetch(url, tier, transport=_mock(handler), **kw)

    monkeypatch.setattr(api_mod, "_fetch", fake_fetch)
    cfg = {
        "url": "https://public.example/",
        "items": {"selector": "li"},
        "fields": {"t": {"selector": "li"}},
        "tier": 1,
        "rate_limit_sec": 0,
        "proxy": "http://169.254.169.254:80",
    }
    guard = _guard(_resolver({"public.example": [PUBLIC_V4]}))
    # R28: a refused proxy is this page's error outcome; the guard still ran
    # before any request, and the outcome does not echo the address (R32a).
    res = scrape(cfg, guard=guard, log=None)
    assert [o.outcome for o in res.page_report] == ["error"]
    assert "private address" in res.page_report[0].note
    assert "169.254.169.254" not in res.page_report[0].note
    assert seen == []


# --- assist.load_page: re-raise under a guard, stay silent (R14) -----------

def test_load_page_walled_site_raises_under_guard(capsys, monkeypatch):
    from certainlyscraped import classify
    from certainlyscraped.assist import load_page

    class R:
        tier = 3
        note = "cloudflare challenge"

    monkeypatch.setattr(classify, "probe", lambda url, *a, **k: R())
    with pytest.raises(FetchError, match="tier 3 disabled under the MCP guard"):
        load_page("https://public.example/", tier="auto", log=None,
                  guard=lambda u: None)
    assert capsys.readouterr().out == ""


def test_load_page_log_none_writes_nothing_to_stdout(capsys, monkeypatch):
    from certainlyscraped import assist, classify

    class R:
        tier = 2
        note = "plain html"

    monkeypatch.setattr(classify, "probe", lambda url, *a, **k: R())
    monkeypatch.setattr(assist, "fetch", lambda url, tier, **kw: (500, "boom"))
    url, html, tier = assist.load_page("https://public.example/", tier="auto",
                                       log=None, guard=lambda u: None)
    assert (url, html, tier) == ("https://public.example/", "boom", 2)
    assert capsys.readouterr().out == ""


def test_load_page_missing_file_log_none_is_silent(tmp_path, capsys):
    from certainlyscraped.assist import load_page

    with pytest.raises(SystemExit):
        load_page(str(tmp_path / "nope.html"), tier="1", log=None)
    assert capsys.readouterr().out == ""
