"""Tiered fetchers. Each tier is one import, lazy-loaded so light installs
only pay for what they use.

tier 1: httpx (plain)
tier 2: curl_cffi (TLS impersonation, verified on AliExpress 08-29)
tier 3: cloakbrowser (source-patched Chromium, Amazon-class) or
        playwright + invisible_playwright fallback
"""
from __future__ import annotations

import importlib.util
import re

from . import __version__

# Tier 1 says who it is. A Chrome UA on a client that does not connect like
# Chrome is what bot filters flag (HN answers it with HTTP 419 on ?p=2), while
# an honest UA gets through. Sites that need a browser identity get tier 2.
USER_AGENT = (f"certainlyscraped/{__version__} "
              "(+https://github.com/vhsgreed/certainlyscraped)")


MAX_REDIRECTS = 10
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# scheme://user:password@ -- the credentials half of a proxy URL in a
# message. The userinfo runs to the LAST '@' in the authority, so a password
# that itself contains '@' is masked whole rather than half-masked.
_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s]*@")


def redact(text: str, secrets=()) -> str:
    """``text`` with every secret, and the credentials of any proxy URL, ``***``.

    Two layers, because a message can carry either: an exact known secret
    (a gateway password, a session token, a configured proxy URL) and the
    userinfo of a URL this process never configured (a client library's own
    error message). ``fetch``/``runner``/``api``/``cli`` all route their
    user-visible text through here before it reaches a log or an outcome.
    """
    if not text:
        return text
    if isinstance(secrets, str):
        secrets = [secrets]
    out = str(text)
    # Longest first: the whole proxy URL must win over its password, or the
    # password is masked and the address (which the URL carried) survives.
    values = sorted({str(secret) for secret in secrets or () if secret},
                    key=len, reverse=True)
    for value in values:
        out = out.replace(value, "***")
    return _CREDENTIALS.sub(r"\1***@", out)


def proxy_url(proxy):
    """A proxy is a URL string or a ``proxypool.Proxy``; clients get the URL.

    R26: the guard checks the address, so a Proxy object must never reach
    ``urlsplit`` (or httpx) -- only ``Proxy.url`` does.
    """
    if proxy is None or isinstance(proxy, str):
        return proxy
    url = getattr(proxy, "url", proxy)
    return url if isinstance(url, str) else proxy


def tier3_available() -> bool:
    """True when any tier-3 backend is installed (scrapling, cloakbrowser or
    playwright). A ladder must not climb to tier 3 it cannot run."""
    return any(importlib.util.find_spec(name) is not None
               for name in ("scrapling", "cloakbrowser", "playwright"))


def fetch(url: str, tier: int, timeout: int = 30, proxy: str | None = None,
          cookies: dict | None = None, *, guard=None,
          transport=None, return_headers: bool = False):
    """Returns (status_code, html), or (status_code, html, headers) when
    ``return_headers`` is true (headers lowercased; used for Retry-After).
    Raises FetchError on hard failure.

    file:// URLs read a saved page from disk (any tier), so a config can be
    developed against a page saved once with `certainlyscraped fetch`.

    guard: callable(url) -> None, e.g. netguard.check_url. When set, tiers 1
    and 2 call the guard on the proxy (if any) before building the client,
    follow redirects by hand and call the guard before every request, so
    neither a public URL nor a public proxy can route the fetcher into an
    internal address. Tier 3 (a browser) cannot be policed per-hop and is
    disabled; file:// is left to the caller, which reads workdir files
    itself.
    transport: test-only httpx transport (httpx.MockTransport).

    proxy may be a URL string or a ``proxypool.Proxy``; only its ``url`` ever
    reaches a client or the guard (R26).
    """
    proxy = proxy_url(proxy)
    if url.startswith("file://"):
        if guard is not None:
            raise FetchError("file:// not allowed under the guard")
        status, html = _read_file(url)
        headers: dict[str, str] = {}
    elif guard is not None:
        if tier not in (1, 2):
            raise FetchError("tier 3 disabled under the MCP guard")
        if tier == 1:
            status, html, headers = _fetch_httpx_guarded(
                url, timeout, proxy, cookies, guard, transport)
        else:
            status, html, headers = _fetch_curl_cffi_guarded(
                url, timeout, proxy, cookies, guard)
    elif tier == 1:
        status, html, headers = _fetch_httpx(url, timeout, proxy, cookies,
                                             transport)
    elif tier == 2:
        status, html, headers = _fetch_curl_cffi(url, timeout, proxy, cookies)
    else:
        status, html = _fetch_browser(url, timeout, proxy, cookies)
        headers = {}
    if return_headers:
        return status, html, headers
    return status, html


class FetchError(RuntimeError):
    pass


def _redirect_location(status: int, headers) -> str | None:
    if status not in _REDIRECT_CODES:
        return None
    return headers.get("Location") or headers.get("location") or None


def _read_file(url: str) -> tuple[int, str]:
    from urllib.parse import unquote, urlparse
    path = unquote(urlparse(url).path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return 200, f.read()
    except FileNotFoundError:
        return 404, ""


def to_url(target: str) -> str:
    """A URL stays a URL; an existing local path becomes a file:// URL."""
    import os
    from pathlib import Path
    if "://" not in target and os.path.exists(target):
        return Path(target).resolve().as_uri()
    return target


def _client_kwargs(timeout, proxy, cookies, transport,
                   follow_redirects) -> dict:
    kwargs = dict(timeout=timeout, follow_redirects=follow_redirects,
                  headers={"User-Agent": USER_AGENT})
    if proxy:
        kwargs["proxy"] = proxy
    if cookies:
        kwargs["cookies"] = cookies
    if transport is not None:
        kwargs["transport"] = transport
    return kwargs


def _lower_headers(headers) -> dict[str, str]:
    """Response headers with lowercase keys; Retry-After must be findable
    whatever case the server sent (HTTP header names are case-insensitive)."""
    try:
        return {str(k).lower(): str(v) for k, v in headers.items()}
    except (AttributeError, TypeError):
        return {}


def _fetch_httpx(url, timeout, proxy, cookies, transport=None):
    import httpx
    if transport is not None:
        with httpx.Client(**_client_kwargs(timeout, proxy, cookies, transport,
                                           follow_redirects=True)) as client:
            r = client.get(url)
        return (r.status_code, r.text,
                _lower_headers(getattr(r, "headers", None)))
    kwargs = dict(timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": USER_AGENT})
    if proxy:
        kwargs["proxy"] = proxy
    if cookies:
        kwargs["cookies"] = cookies
    r = httpx.get(url, **kwargs)
    return (r.status_code, r.text,
            _lower_headers(getattr(r, "headers", None)))


def _guard_proxy(proxy, guard) -> None:
    """A proxy is a request destination too: 127.0.0.1:2375 (a Docker
    socket), 169.254.169.254 (metadata), an internal squid. Guarding only
    the target URL lets a config keep `url` public and aim `proxy` inward."""
    if proxy:
        guard(proxy)


def _fetch_httpx_guarded(url, timeout, proxy, cookies, guard, transport):
    """Tier 1 with redirects followed one hop at a time, guard first."""
    import httpx
    from urllib.parse import urljoin

    _guard_proxy(proxy, guard)
    with httpx.Client(**_client_kwargs(timeout, proxy, cookies, transport,
                                       follow_redirects=False)) as client:
        return _guarded_get(client.get, url, guard, urljoin,
                            lambda r: (r.status_code, r.text, r.headers))


def _fetch_curl_cffi_guarded(url, timeout, proxy, cookies, guard):
    """Tier 2 with the same hand-rolled redirect loop as tier 1."""
    from urllib.parse import urljoin

    try:
        from curl_cffi import requests as cr
    except ImportError as e:
        raise FetchError("tier 2 needs curl_cffi: pip install curl_cffi") from e
    _guard_proxy(proxy, guard)
    kwargs = dict(impersonate="chrome", timeout=timeout, allow_redirects=False)
    if proxy:
        kwargs["proxy"] = proxy
    if cookies:
        kwargs["cookies"] = cookies

    def get(u):
        return cr.get(u, **kwargs)

    return _guarded_get(get, url, guard, urljoin,
                        lambda r: (r.status_code, r.text, r.headers))


def _guarded_get(get, url, guard, urljoin, unpack):
    """One guarded request per hop, never more than MAX_REDIRECTS hops."""
    current = url
    redirects = 0
    while True:
        # Check the exact URL about to be requested, not just the first one.
        guard(current)
        r = get(current)
        status, text, headers = unpack(r)
        location = _redirect_location(status, headers)
        if location is None:
            return status, text, _lower_headers(headers)
        if redirects >= MAX_REDIRECTS:
            raise FetchError("too many redirects")
        redirects += 1
        current = urljoin(current, location)


def _fetch_curl_cffi(url, timeout, proxy, cookies):
    try:
        from curl_cffi import requests as cr
    except ImportError as e:
        raise FetchError("tier 2 needs curl_cffi: pip install curl_cffi") from e
    kwargs = dict(impersonate="chrome", timeout=timeout)
    if proxy:
        kwargs["proxy"] = proxy
    if cookies:
        kwargs["cookies"] = cookies
    r = cr.get(url, **kwargs)
    return (r.status_code, r.text,
            _lower_headers(getattr(r, "headers", None)))


def _cookie_params(url: str, cookies: dict | None) -> list[dict]:
    """Config cookies ({name: value}) as Playwright cookie params."""
    return [{"name": k, "value": str(v), "url": url}
            for k, v in (cookies or {}).items()]


def _playwright_proxy(proxy: str | None) -> dict | None:
    """'http://user:pass@host:port' -> Playwright's proxy dict."""
    if not proxy:
        return None
    from urllib.parse import urlparse
    u = urlparse(proxy)
    out = {"server": f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")}
    if u.username:
        out["username"] = u.username
    if u.password:
        out["password"] = u.password
    return out


def _fetch_browser(url, timeout, proxy, cookies) -> tuple[int, str]:
    """Amazon/eBay-class. Try scrapling (patchright stealth + cloudflare
    solve), then cloakbrowser, then plain playwright. None are magic; the
    probe-first flow decides whether tier 3 is even offered.

    Every backend honours proxy, cookies and timeout, and returns the real
    HTTP status of the navigation (0 when the browser reports none), so a
    403 challenge page is not passed off as a 200."""
    try:
        from scrapling.fetchers import StealthySession
    except ImportError:
        pass
    else:
        kwargs = dict(headless=True, timeout=timeout * 1000)
        if proxy:
            kwargs["proxy"] = proxy
        if cookies:
            kwargs["cookies"] = _cookie_params(url, cookies)
        with StealthySession(**kwargs) as session:
            page = session.fetch(url, solve_cloudflare=True)
            return page.status, str(page.html_content)

    try:
        from cloakbrowser import launch
    except ImportError:
        pass
    else:
        ctx = launch(headless=True, proxy=proxy)
        try:
            page = ctx.new_page()
            if cookies:
                page.context.add_cookies(_cookie_params(url, cookies))
            resp = page.goto(url, timeout=timeout * 1000,
                             wait_until="domcontentloaded")
            return (resp.status if resp else 0), page.content()
        finally:
            ctx.close()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FetchError("tier 3 needs scrapling or cloakbrowser or playwright "
                         "+ stealth deps") from e
    with sync_playwright() as p:
        launch_kwargs = {"headless": True}
        pw_proxy = _playwright_proxy(proxy)
        if pw_proxy:
            launch_kwargs["proxy"] = pw_proxy
        browser = p.chromium.launch(**launch_kwargs)
        try:
            ctx = browser.new_context()
            if cookies:
                ctx.add_cookies(_cookie_params(url, cookies))
            page = ctx.new_page()
            resp = page.goto(url, timeout=timeout * 1000,
                             wait_until="domcontentloaded")
            return (resp.status if resp else 0), page.content()
        finally:
            browser.close()
