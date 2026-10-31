"""Proxy pool (spec section 5). No network: every fetch is scripted.

Run: python3 -m pytest tests/ -q
"""
from __future__ import annotations

import io
import json
import re
import sys
import threading

sys.path.insert(0, ".")

import pytest

import certainlyscraped.api as api
import certainlyscraped.cli as cli
from certainlyscraped.api import ScrapeResult
from certainlyscraped.fetch import fetch as real_fetch
from certainlyscraped.fetch import redact
from certainlyscraped.proxypool import Proxy, ProxyPool

FIX = "tests/fixtures"
LINES = [
    "http://u:p@10.1.2.3:8000 # dc-a",
    "http://10.1.9.9:8000",
    "http://10.2.0.1:8000",
    "http://[2001:db8:1::5]:8000",
    "http://proxy.example:3128",
]
GATEWAY = {
    "template": "http://{user}:{pass}@{host}:{port}",
    "host": "gw.example",
    "port": 7000,
    "user": "me",
    "password_env": "GATEWAY_PASSWORD",
    "session": "rotate",
    "label": "gw",
}
GATEWAY_SESSION = dict(GATEWAY, template="http://{user}-{session}:{pass}@gw:7000")

OK = "<html><body>" + ("nice real content " * 200) + "</body></html>"


class FakeClock:
    """Monotonic clock whose `advance` moves time without blocking."""

    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


# --- groups ---------------------------------------------------------------

def test_groups_from_labels_and_prefixes():
    pool = ProxyPool.from_config({"proxies": LINES}, env={})
    assert pool.groups() == ["dc-a", "10.1/16", "10.2/16",
                             "2001:db8:1::/48", "proxy.example"]
    # the url keeps its credentials: only the *repr* hides them
    assert pool.acquire("a.com").url == "http://u:p@10.1.2.3:8000"


def test_groups_from_a_proxies_file():
    pool = ProxyPool.from_config(
        {"proxies": {"list": "proxies.txt"}}, env={}, base_dir=FIX)
    assert pool.groups() == ["dc-a", "10.1/16", "10.2/16",
                             "2001:db8:1::/48", "proxy.example"]


def test_groups_from_a_mapping():
    pool = ProxyPool.from_config(
        {"proxies": {"dc-a": ["http://a1:1", "http://a2:1"],
                     "dc-b": ["http://b1:1"]}}, env={})
    assert pool.groups() == ["dc-a", "dc-b"]
    # Round-robin over groups; each group rotates through its own urls.
    assert [pool.acquire("h").url for _ in range(4)] == [
        "http://a1:1", "http://b1:1", "http://a2:1", "http://b1:1"]


def test_single_proxy_string_is_one_group():
    pool = ProxyPool.from_config({"proxy": "http://single:3128"}, env={})
    assert pool.groups() == ["default"]
    assert pool.acquire("a.com").url == "http://single:3128"


def test_no_proxy_config_means_no_pool():
    assert ProxyPool.from_config({"url": "https://x.test/"}, env={}) is None


def test_env_override_replaces_the_config_list():
    pool = ProxyPool.from_config(
        {"proxies": ["http://config:1"]},
        env={"CERTAINLYSCRAPED_PROXIES": "http://env:1 # e"})
    assert pool.groups() == ["e"]


def test_repr_never_shows_the_address():
    proxy = Proxy("http://u:s3cret@10.1.2.3:8000", "dc-a")
    assert repr(proxy) == "Proxy(group=dc-a)"
    assert str(proxy) == "Proxy(group=dc-a)"
    assert "s3cret" not in repr(proxy) and "10.1.2.3" not in repr(proxy)


# --- cooldown -------------------------------------------------------------

def test_cooldown_is_per_host():
    clock = FakeClock()
    pool = ProxyPool.from_config({"proxies": ["http://p1:1"]}, env={},
                                 clock=clock.clock)
    p1 = pool.acquire("a.com")
    pool.report(p1, "a.com", "blocked")
    assert pool.acquire("a.com") is None
    # The same proxy is still fine for another host.
    assert pool.acquire("b.com").url == "http://p1:1"
    clock.advance(299)
    assert pool.acquire("a.com") is None
    clock.advance(1)
    assert pool.acquire("a.com") is not None


def test_ok_clears_the_cooldown():
    clock = FakeClock()
    pool = ProxyPool.from_config({"proxies": ["http://p1:1"]}, env={},
                                 clock=clock.clock)
    p1 = pool.acquire("a.com")
    pool.report(p1, "a.com", "rate_limited")
    assert pool.acquire("a.com") is None
    pool.report(p1, "a.com", "ok")
    assert pool.acquire("a.com") is not None


def test_cooldown_defaults_to_five_minutes():
    clock = FakeClock()
    pool = ProxyPool.from_config({"proxies": ["http://p1:1"]}, env={},
                                 clock=clock.clock)
    assert pool.cooldown_sec == 300
    p1 = pool.acquire("a.com")
    pool.report(p1, "a.com", "blocked")
    clock.advance(300)
    assert pool.acquire("a.com") is not None


def test_alternate_skips_tried_groups():
    pool = ProxyPool.from_config(
        {"proxies": ["http://a1:1 # a", "http://b1:1 # b",
                     "http://c1:1 # c"]}, env={})
    assert pool.acquire("h.com").group == "a"
    assert pool.alternate("h.com", {"a"}).group == "b"
    assert pool.alternate("h.com", {"a", "b"}).group == "c"
    assert pool.alternate("h.com", {"a", "b", "c"}) is None


def test_alternate_skips_a_cooling_group():
    clock = FakeClock()
    pool = ProxyPool.from_config(
        {"proxies": ["http://a1:1 # a", "http://b1:1 # b"]}, env={},
        clock=clock.clock)
    a = pool.acquire("h.com")
    pool.report(a, "h.com", "blocked")
    assert pool.acquire("h.com").group == "b"


def test_acquire_is_thread_safe():
    pool = ProxyPool.from_config({"proxies": ["http://p:1"]}, env={})
    p = pool.acquire("a.com")
    pool.report(p, "a.com", "blocked")
    seen = []

    def worker():
        for _ in range(50):
            seen.append(pool.acquire("a.com"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen and all(item is None for item in seen)


# --- gateway templates ----------------------------------------------------

def test_gateway_template_and_env_password():
    pool = ProxyPool.from_config({"proxies": {"gateway": GATEWAY}},
                                 env={"GATEWAY_PASSWORD": "s3cret"})
    proxy = pool.acquire("a.com")
    assert proxy.url == "http://me:s3cret@gw.example:7000"
    assert proxy.group == "gw"
    assert repr(proxy) == str(proxy) == "Proxy(group=gw)"
    assert "s3cret" not in repr(proxy)
    assert "gw.example" not in repr(proxy)
    assert "s3cret" in pool.secrets()


def test_gateway_missing_env_names_the_variable():
    with pytest.raises(ValueError) as exc:
        ProxyPool.from_config({"proxies": {"gateway": GATEWAY}}, env={})
    assert "GATEWAY_PASSWORD" in str(exc.value)
    assert "s3cret" not in str(exc.value)


def test_gateway_rotate_generates_a_session_per_acquire():
    pool = ProxyPool.from_config({"proxies": {"gateway": GATEWAY_SESSION}},
                                 env={"GATEWAY_PASSWORD": "p"})
    first, second = pool.acquire("a.com"), pool.acquire("a.com")
    tokens = [re.search(r"me-([0-9a-f]{8}):", p.url).group(1)
              for p in (first, second)]
    assert tokens[0] != tokens[1]
    for token in tokens:
        assert token in pool.secrets()


def test_gateway_sticky_session_is_per_host():
    pool = ProxyPool.from_config(
        {"proxies": {"gateway": dict(GATEWAY_SESSION, session="sticky")}},
        env={"GATEWAY_PASSWORD": "p"})
    a1, a2, b = pool.acquire("a.com"), pool.acquire("a.com"), pool.acquire("b.com")
    assert a1.url == a2.url
    assert a1.url != b.url


def test_gateway_refuses_a_password_in_the_config():
    with pytest.raises(ValueError) as exc:
        ProxyPool.from_config(
            {"proxies": {"gateway": dict(GATEWAY, password="s3cret")}}, env={})
    assert "password_env" in str(exc.value)
    assert "s3cret" not in str(exc.value)


# --- redaction ------------------------------------------------------------

def test_redact_masks_a_password_everywhere():
    assert redact("boom s3cret boom", ["s3cret"]) == "boom *** boom"


def test_redact_masks_the_userinfo_of_a_proxy_url():
    text = "connect to http://u:s3cret@h:1 failed"
    assert redact(text, []) == "connect to http://***@h:1 failed"
    assert redact(text, ["s3cret"]) == "connect to http://***@h:1 failed"


def test_pool_scrub_hides_the_whole_proxy_address():
    pool = ProxyPool.from_config(
        {"proxies": ["http://u:s3cret@10.1.2.3:8000"]}, env={})
    text = pool.scrub("ProxyError: http://u:s3cret@10.1.2.3:8000 refused")
    assert "s3cret" not in text and "10.1.2.3" not in text


# --- fetch() accepts a Proxy ---------------------------------------------

def test_fetch_passes_the_proxy_url_to_the_client(monkeypatch):
    import httpx
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            return httpx.Response(200, text="ok")

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient(**kw))
    proxy = Proxy("http://u:p@10.0.0.1:8000", "dc-a")
    status, html = real_fetch("https://h.test/1", 1, proxy=proxy,
                              guard=lambda url: None)
    assert (status, html) == (200, "ok")
    assert seen["proxy"] == "http://u:p@10.0.0.1:8000"


def test_fetch_guards_the_proxy_address_not_the_object(monkeypatch):
    import httpx
    guarded = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            return httpx.Response(200, text="ok")

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient(**kw))
    proxy = Proxy("http://10.0.0.1:8000", "dc-a")
    real_fetch("https://h.test/1", 1, proxy=proxy,
               guard=lambda url: guarded.append(url))
    assert guarded == ["http://10.0.0.1:8000", "https://h.test/1"]


# --- secret hygiene through the API and the CLI ---------------------------

PROXY_URL = "http://u:s3cret@10.1.2.3:8000"


def test_no_secret_anywhere(tmp_path, monkeypatch, capsys):
    import httpx

    def boom(url, tier, **kwargs):
        raise httpx.ProxyError(f"{PROXY_URL} refused")

    monkeypatch.setattr(api, "_fetch", boom)
    cfg = {"tier": 1, "rate_limit_sec": 0, "concurrency": 1,
           "url": "https://h.test/1", "urls": ["https://h.test/1"],
           "proxies": [PROXY_URL],
           "items": {"selector": "li"},
           "fields": {"t": {"selector": "a"}}}
    log = io.StringIO()
    result = api.scrape(cfg, log=log)

    text = log.getvalue()
    assert "s3cret" not in text and "10.1.2.3" not in text
    assert result.page_report and result.page_report[0].outcome == "error"
    dumped = json.dumps([o.to_dict() for o in result.page_report])
    assert "s3cret" not in dumped and "10.1.2.3" not in dumped

    # ... and through the CLI, including the --report file.
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        "url: https://h.test/1\n"
        "urls: [https://h.test/1]\n"
        "tier: 1\n"
        "rate_limit_sec: 0\n"
        "concurrency: 1\n"
        "proxies:\n"
        f"  - {PROXY_URL}\n"
        "items: {selector: li}\n"
        "fields: {t: {selector: a}}\n", encoding="utf-8")
    report = tmp_path / "report.jsonl"
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", str(cfg_path), "--out", str(tmp_path / "o.csv"),
                  "--report", str(report)])
    assert exc.value.code != 0
    body = report.read_text(encoding="utf-8")
    assert "s3cret" not in body and "10.1.2.3" not in body
    assert "***" in body
    stderr = capsys.readouterr().err
    assert "s3cret" not in stderr and "10.1.2.3" not in stderr


def test_mcp_refuses_a_proxy_config(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from mcp import Client

    from certainlyscraped.mcp_server import build_server

    async def go():
        async with Client(build_server(tmp_path)) as client:
            return await client.call_tool("scrape", {
                "config": ("url: https://h.test/1\n"
                           f"proxies: [{PROXY_URL}]\n"
                           "items: {selector: li}\n")})

    result = asyncio.run(go())
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "error"
    assert "s3cret" not in json.dumps(payload)
    assert "10.1.2.3" not in json.dumps(payload)


# --- CLI flags ------------------------------------------------------------

def _fake_scrape(seen):
    def fake(cfg, log=None):
        seen.update(cfg)
        return ScrapeResult(rows=[{"a": 1}], pages=1, tier=1,
                            config=dict(cfg))
    return fake


def test_run_proxies_flag_overrides_the_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("url: https://h.test/1\nproxy: http://old:1\n"
                        "items: {selector: li}\n", encoding="utf-8")
    proxies = tmp_path / "proxies.txt"
    proxies.write_text("# one per line, optional label\n"
                       "http://p1:1 # dc-a\nhttp://p2:1\n",
                       encoding="utf-8")
    seen = {}
    monkeypatch.setattr(cli, "scrape", _fake_scrape(seen))
    cli.main(["run", str(cfg_path), "--proxies", str(proxies),
              "--out", str(tmp_path / "o.csv")])
    assert seen["proxies"] == ["http://p1:1 # dc-a", "http://p2:1"]
    assert "proxy" not in seen  # the file wins over the single proxy
    pool = ProxyPool.from_config(seen, env={})
    assert pool.groups() == ["dc-a", "p2"]


def test_run_no_proxy_disables_the_pool(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("url: https://h.test/1\nproxy: http://p:1\n"
                        "items: {selector: li}\n", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(cli, "scrape", _fake_scrape(seen))
    cli.main(["run", str(cfg_path), "--no-proxy",
              "--out", str(tmp_path / "o.csv")])
    assert "proxy" not in seen and "proxies" not in seen


def test_proxies_file_accepts_a_yaml_mapping(tmp_path):
    from certainlyscraped.proxypool import read_proxies_file

    path = tmp_path / "p.yaml"
    path.write_text("dc-a:\n  - http://a1:1\ndc-b:\n  - http://b1:1\n",
                    encoding="utf-8")
    assert read_proxies_file(str(path)) == {"dc-a": ["http://a1:1"],
                                            "dc-b": ["http://b1:1"]}


# --- state does not leak between scrapes ----------------------------------

def test_each_scrape_builds_its_own_pool(monkeypatch):
    built = []

    class RecordingPool:
        @classmethod
        def from_config(cls, cfg, **kwargs):
            pool = cls()
            built.append(pool)
            return pool

        def acquire(self, host):
            return None

        def alternate(self, host, exclude_groups):
            return None

        def report(self, proxy, host, outcome):
            pass

        def secrets(self):
            return []

    monkeypatch.setattr(api, "ProxyPool", RecordingPool)
    monkeypatch.setattr(api, "_fetch", lambda *a, **k: (200, OK))
    cfg = {"tier": 1, "rate_limit_sec": 0, "url": "https://h.test/1",
           "urls": ["https://h.test/1"], "proxies": ["http://p:1"],
           "items": {"selector": "li"},
           "fields": {"t": {"selector": "a"}}}
    api.scrape(cfg, log=None)
    api.scrape(cfg, log=None)
    assert len(built) == 2 and built[0] is not built[1]
