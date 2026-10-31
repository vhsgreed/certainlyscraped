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
    "http://10.1.2.4:8000 # dc-a",
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
    # R46: an unlabelled proxy's group is a neutral "net-N"; no address or
    # network leaks into the label. User labels are kept as written.
    pool = ProxyPool.from_config({"proxies": LINES}, env={})
    assert pool.groups() == ["dc-a", "net-1", "net-2", "net-3", "net-4"]
    assert pool.acquire("a.com").url == "http://10.1.2.4:8000"


def test_groups_from_a_proxies_file():
    pool = ProxyPool.from_config(
        {"proxies": {"list": "proxies.txt"}}, env={}, base_dir=FIX)
    assert pool.groups() == ["dc-a", "net-1", "net-2", "net-3", "net-4"]


def test_unlabelled_proxies_are_grouped_by_network():
    """Two proxies in one /16 share a derived group; another /16 is net-2."""
    pool = ProxyPool.from_config(
        {"proxies": ["http://10.1.2.3:8000",
                     "http://10.1.9.9:8000",
                     "http://10.2.3.4:8000"]}, env={})
    assert pool is not None
    assert pool.groups() == ["net-1", "net-2"]
    # exclude the other group to read one group's members without a rotation
    one, two = pool.alternate("h.test", {"net-2"}), \
        pool.alternate("h.test", {"net-2"})
    other = pool.alternate("h.test", {"net-1"})
    assert one is not None and two is not None and other is not None
    assert {one.group, two.group} == {"net-1"}
    assert {one.url, two.url} == {"http://10.1.2.3:8000",
                                  "http://10.1.9.9:8000"}
    assert (other.group, other.url) == ("net-2", "http://10.2.3.4:8000")


def test_derived_group_labels_are_neutral():
    """R46: no hostname and no /16 reaches steps or proxy_group."""
    pool = ProxyPool.from_config(
        {"proxies": ["http://squid.internal.corp:3128",
                     "http://10.1.2.3:8000",
                     "http://10.2.3.4:8000"]}, env={})
    assert pool.groups() == ["net-1", "net-2", "net-3"]
    for label in pool.groups():
        assert "squid" not in label and "10." not in label
        assert "/" not in label


def test_user_group_labels_are_kept():
    pool = ProxyPool.from_config(
        {"proxies": ["http://a:1 # dc-a", "http://10.1.2.3:8000"]}, env={})
    assert pool.groups() == ["dc-a", "net-1"]


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


def test_gateway_sticky_session_is_per_worker():
    """Spec s5: sticky keeps one session per (host, worker) -- R30."""
    pool = ProxyPool.from_config(
        {"proxies": {"gateway": dict(GATEWAY_SESSION, session="sticky")}},
        env={"GATEWAY_PASSWORD": "p"})

    def token(url):
        return re.search(r"me-([0-9a-f]{8}):", url).group(1)

    # One thread (this one) gets the same token again and again for a host.
    same = [token(pool.acquire("a.com").url) for _ in range(3)]
    assert len(set(same)) == 1

    # Eight live threads (a barrier keeps their idents from being reused)
    # each get their own session for that same host.
    barrier = threading.Barrier(8)
    seen = []

    def worker():
        barrier.wait()
        seen.append(token(pool.acquire("a.com").url))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 8 and len(set(seen)) == 8
    assert set(seen) & set(same) == set()


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


def test_pool_scrub_hides_the_whole_proxy_address(tmp_path):
    proxies = tmp_path / "proxies.txt"
    proxies.write_text("http://u:s3cret@10.1.2.3:8000\n", encoding="utf-8")
    pool = ProxyPool.from_config({"proxies": {"list": str(proxies)}}, env={})
    assert "s3cret" not in pool.scrub("ProxyError: http://u:s3cret@10.1.2.3:8000"
                                     " refused")
    assert "10.1.2.3" not in pool.scrub("ProxyError: http://u:s3cret@10.1.2.3:8000"
                                        " refused")


def test_pool_scrub_hides_the_bare_host_and_host_port():
    """R32a: netguard says 'private address: 10.1.2.3' -- no scheme, no port."""
    pool = ProxyPool.from_config(
        {"proxies": ["http://10.1.2.3:8000", "http://[2001:db8:1::5]:8000",
                     "http://proxy.example:3128"]}, env={})
    for host in ("10.1.2.3", "10.1.2.3:8000", "2001:db8:1::5",
                 "proxy.example"):
        assert pool.scrub(f"private address: {host}") == "private address: ***"


def test_redact_masks_a_password_that_contains_at():
    """R32e: a userinfo password may itself contain '@'."""
    text = "ProxyError: http://u:p@ss@h:1 refused"
    assert redact(text, []) == "ProxyError: http://***@h:1 refused"


def test_gateway_percent_encodes_its_credentials():
    """R32b: '@' in a password must not split the URL's userinfo."""
    pool = ProxyPool.from_config(
        {"proxies": {"gateway": dict(GATEWAY, template=(
            "http://{user}-{session}:{pass}@{host}:{port}"), password_env="GW")}},
        env={"GW": "p@ss:w/rd"})
    proxy = pool.acquire("a.com")
    assert "p@ss" not in proxy.url and "p%40ss" in proxy.url
    scrubbed = pool.scrub(f"ProxyError: {proxy.url} refused")
    assert "p@ss" not in scrubbed and "%40ss" not in scrubbed
    assert "gw.example" not in scrubbed and "me-" not in scrubbed


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
    # A password belongs in a proxies file, never in the main config (R31).
    proxies = tmp_path / "proxies.txt"
    proxies.write_text(PROXY_URL + "\n", encoding="utf-8")
    cfg = {"tier": 1, "rate_limit_sec": 0, "concurrency": 1,
           "url": "https://h.test/1", "urls": ["https://h.test/1"],
           "proxies": {"list": str(proxies)},
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
        "items: {selector: li}\n"
        "fields: {t: {selector: a}}\n", encoding="utf-8")
    report = tmp_path / "report.jsonl"
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", str(cfg_path), "--proxies", str(proxies),
                  "--out", str(tmp_path / "o.csv"),
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
    def fake(cfg, log=None, **kwargs):
        seen.update(cfg)
        seen["_kwargs"] = kwargs
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
    assert pool.groups() == ["dc-a", "net-1"]


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


# --- R31: passwords in the main config are refused ------------------------

def test_main_config_rejects_a_password_in_a_proxy_list():
    with pytest.raises(ValueError) as exc:
        ProxyPool.from_config({"proxies": [PROXY_URL]}, env={})
    message = str(exc.value)
    assert "--proxies" in message and "password_env" in message
    assert "s3cret" not in message and "10.1.2.3" not in message


def test_main_config_rejects_a_password_in_a_single_proxy():
    with pytest.raises(ValueError) as exc:
        ProxyPool.from_config({"proxy": PROXY_URL}, env={})
    assert "--proxies" in str(exc.value)
    assert "s3cret" not in str(exc.value)


def test_a_proxies_file_may_carry_a_password(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text(PROXY_URL + " # dc-a\n", encoding="utf-8")
    pool = ProxyPool.from_config({"proxies": {"list": str(path)}}, env={})
    assert pool.acquire("h.test").url == PROXY_URL


def test_the_env_var_may_carry_a_password():
    pool = ProxyPool.from_config(
        {}, env={"CERTAINLYSCRAPED_PROXIES": PROXY_URL})
    assert pool.acquire("h.test").url == PROXY_URL


def test_cli_rejects_a_config_password_and_names_the_alternative(
        tmp_path, capsys):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("url: https://h.test/1\n"
                        f"proxy: {PROXY_URL}\n"
                        "items: {selector: li}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", str(cfg_path), "--out", str(tmp_path / "o.csv")])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--proxies" in err
    assert "s3cret" not in err and "10.1.2.3" not in err


# --- R29: the MCP server takes a pool, never an agent proxy ---------------

MCP_CONFIG = ("url: https://h.test/1\n"
              "tier: 1\n"
              "items: {selector: li}\n"
              "fields: {t: {selector: a}}\n")


def test_mcp_server_uses_the_pool_it_was_built_with(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from mcp import Client

    from certainlyscraped.mcp_server import build_server

    seen = []

    def fake_fetch(url, tier, **kwargs):
        seen.append(kwargs.get("proxy"))
        return 200, OK

    monkeypatch.setattr(api, "_fetch", fake_fetch)
    pool = ProxyPool.from_config({"proxies": ["http://1.1.1.1:3128 # dc"]},
                                 env={})

    async def go():
        async with Client(build_server(tmp_path, pool=pool)) as client:
            return await client.call_tool("scrape", {"config": MCP_CONFIG})

    payload = json.loads(asyncio.run(go()).content[0].text)
    assert payload["status"] in ("complete", "too_few_rows"), payload
    assert seen == ["http://1.1.1.1:3128"]


def test_mcp_server_still_refuses_an_agent_supplied_proxy(tmp_path,
                                                          monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from mcp import Client

    from certainlyscraped.mcp_server import build_server

    def fake_fetch(url, tier, **kwargs):  # pragma: no cover
        raise AssertionError("the fetch must never run")

    monkeypatch.setattr(api, "_fetch", fake_fetch)
    pool = ProxyPool.from_config({"proxies": ["http://1.1.1.1:3128 # dc"]},
                                 env={})
    text = (MCP_CONFIG + "proxies: [http://2.2.2.2:3128]\n")

    async def go():
        async with Client(build_server(tmp_path, pool=pool)) as client:
            return await client.call_tool("scrape", {"config": text})

    payload = json.loads(asyncio.run(go()).content[0].text)
    assert payload["status"] == "error"
    assert "refused" in " ".join(payload["notes"])


# --- item 6: the proxies block of spec section 5 --------------------------

def test_spec_s5_proxies_block(tmp_path):
    (tmp_path / "proxies.txt").write_text("http://10.1.9.9:8000\n",
                                          encoding="utf-8")
    cfg = {"proxies": {
        "list": "proxies.txt",
        "gateway": {
            "url": ("http://{user}-country-{country}-session-{session}:"
                    "{password}@gw.example:7000"),
            "user": "myuser",
            "password_env": "GATEWAY_PASSWORD",
            "country": "se",
            "session": "rotate",
        },
        "per_proxy_rate_sec": 1.0,
        "cooldown_sec": 42,
        "max_retries": 3,
    }}
    pool = ProxyPool.from_config(cfg, env={"GATEWAY_PASSWORD": "pw"},
                                 base_dir=str(tmp_path))
    assert pool.groups() == ["net-1", "gateway"]
    assert pool.cooldown_sec == 42


def test_ladder_reads_tuning_from_the_proxies_block():
    from certainlyscraped.escalate import Ladder

    cfg = {"proxies": {"list": "x.txt", "per_proxy_rate_sec": 2.5,
                       "max_retries": 2},
           "rate_limit_sec": 9, "max_retries": 0}
    ladder = Ladder(cfg, 1, fetch_fn=lambda *a, **k: (200, ""),
                    sleep=lambda s: None)
    assert ladder.gate.interval == 2.5
    assert ladder.max_retries == 2


def test_reserved_list_key_must_be_a_file_path():
    with pytest.raises(ValueError) as exc:
        ProxyPool.from_config({"proxies": {"list": ["http://a:1"]}}, env={})
    assert "list" in str(exc.value) and "reserved" in str(exc.value)


def test_reserved_gateway_key_must_be_a_mapping():
    with pytest.raises(ValueError) as exc:
        ProxyPool.from_config({"proxies": {"gateway": ["http://a:1"]}}, env={})
    assert "gateway" in str(exc.value) and "reserved" in str(exc.value)


# --- item 7: the --proxies file reader ------------------------------------

def test_proxies_file_block_list_keeps_labels(tmp_path):
    from certainlyscraped.proxypool import read_proxies_file

    path = tmp_path / "p.txt"
    path.write_text("- http://p1:1 # dc-a\n- http://p2:1\n", encoding="utf-8")
    assert read_proxies_file(str(path)) == ["http://p1:1 # dc-a",
                                            "http://p2:1"]
    pool = ProxyPool.from_config({"proxies": {"list": str(path)}}, env={})
    assert pool.groups() == ["dc-a", "net-1"]


def test_proxies_file_flow_list(tmp_path):
    from certainlyscraped.proxypool import read_proxies_file

    path = tmp_path / "p.txt"
    path.write_text("[http://p1:1, http://p2:1]\n", encoding="utf-8")
    assert read_proxies_file(str(path)) == ["http://p1:1", "http://p2:1"]


def test_proxies_file_mapping_with_a_single_string_value(tmp_path):
    from certainlyscraped.proxypool import read_proxies_file

    path = tmp_path / "p.yaml"
    path.write_text("dc-a: http://a1:1\n", encoding="utf-8")
    assert read_proxies_file(str(path)) == {"dc-a": "http://a1:1"}
    pool = ProxyPool.from_config({"proxies": read_proxies_file(str(path))},
                                 env={})
    assert pool.groups() == ["dc-a"]


def test_proxies_file_rejects_a_bad_line_with_its_number(tmp_path):
    from certainlyscraped.proxypool import read_proxies_file

    path = tmp_path / "p.txt"
    path.write_text("http://p1:1\nnot-a-proxy\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        read_proxies_file(str(path))
    assert "line 2" in str(exc.value)


# --- item 8: --no-proxy is absolute --------------------------------------

def test_use_env_false_ignores_the_environment():
    cfg = {"proxies": ["http://cfg:1"]}
    env = {"CERTAINLYSCRAPED_PROXIES": "http://env:1"}
    off = ProxyPool.from_config(cfg, env=env, use_env=False)
    # R46: derived labels are neutral, so the label alone proves nothing:
    # the address has to be the config's, and the environment's absent.
    assert off is not None
    assert off.groups() == ["net-1"]
    proxy = off.acquire("h.test")
    assert proxy is not None and proxy.url == "http://cfg:1"
    on = ProxyPool.from_config(cfg, env=env, use_env=True)
    assert on is not None
    proxy = on.acquire("h.test")
    assert proxy is not None and proxy.url == "http://env:1"


def test_no_proxy_beats_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("CERTAINLYSCRAPED_PROXIES", "http://env:1")
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("url: https://h.test/1\nitems: {selector: li}\n",
                        encoding="utf-8")
    seen = {}

    def fake(cfg, log=None, **kwargs):
        seen["pool"] = ProxyPool.from_config(
            cfg, use_env=kwargs.get("use_env", True))
        return ScrapeResult(rows=[{"a": 1}], pages=1, tier=1, config=dict(cfg))

    monkeypatch.setattr(cli, "scrape", fake)
    cli.main(["run", str(cfg_path), "--no-proxy",
              "--out", str(tmp_path / "o.csv")])
    assert seen["pool"] is None


# --- R32a: the guard's bare-host message --------------------------------

def test_guard_message_does_not_leak_the_proxy_host(tmp_path, monkeypatch):
    from certainlyscraped.netguard import check_url

    proxies = tmp_path / "proxies.txt"
    proxies.write_text(PROXY_URL + "\n", encoding="utf-8")

    def guarded_fetch(url, tier, *, guard=None, proxy=None, **kwargs):
        guard(proxy)  # raises: private address: 10.1.2.3
        return 200, OK

    monkeypatch.setattr(api, "_fetch", guarded_fetch)
    cfg = {"tier": 1, "rate_limit_sec": 0, "concurrency": 1,
           "url": "https://h.test/1", "urls": ["https://h.test/1"],
           "proxies": {"list": str(proxies)},
           "items": {"selector": "li"}, "fields": {"t": {"selector": "a"}}}
    log = io.StringIO()
    result = api.scrape(cfg, log=log, guard=check_url)

    for text in (log.getvalue(), result.page_report[0].note):
        assert "10.1.2.3" not in text and "s3cret" not in text
    assert "***" in result.page_report[0].note
    report = tmp_path / "report.jsonl"
    cli.write_report(str(report), result.page_report)
    body = report.read_text(encoding="utf-8")
    assert "10.1.2.3" not in body and "s3cret" not in body


# --- R28: the links path turns a fetch exception into an outcome ----------

def test_links_fetch_exception_is_an_error_outcome(tmp_path):
    import httpx

    calls = []

    def fetcher(url):
        calls.append(url)
        if len(calls) == 1:
            return 200, ('<html><body><a class="next" href="/2">next</a>'
                         "</body></html>")
        raise httpx.ProxyError("http://user:TOKEN@10.1.2.3:8000 refused")

    cfg = {"url": "https://h.test/1", "paginate": {"selector": "a.next"},
           "rate_limit_sec": 0, "items": {"selector": "li"},
           "fields": {"t": {"selector": "a"}}}
    log = io.StringIO()
    result = api.scrape(cfg, fetcher=fetcher, log=log)
    assert len(calls) == 2
    assert result.page_report[-1].outcome == "error"
    assert "TOKEN" not in log.getvalue()
    assert "[paginate]" in log.getvalue()
    dumped = json.dumps([o.to_dict() for o in result.page_report])
    assert "TOKEN" not in dumped
    report = tmp_path / "report.jsonl"
    cli.write_report(str(report), result.page_report)
    assert "TOKEN" not in report.read_text(encoding="utf-8")


# --- item 10c: base_dir defaults to the config file's directory -----------

def test_scrape_uses_the_config_directory_as_base_dir(tmp_path, monkeypatch):
    (tmp_path / "proxies.txt").write_text("http://10.8.8.8:8000 # n\n",
                                          encoding="utf-8")
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("url: https://h.test/1\ntier: 1\n"
                        "rate_limit_sec: 0\n"
                        "proxies:\n  list: proxies.txt\n"
                        "items: {selector: li}\n"
                        "fields: {t: {selector: a}}\n", encoding="utf-8")
    monkeypatch.setattr(api, "_fetch", lambda *a, **k: (200, OK))
    result = api.scrape(str(cfg_path), log=None)
    assert result.page_report[0].proxy_group == "n"


def test_cli_passes_the_config_directory_as_base_dir(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("url: https://h.test/1\nitems: {selector: li}\n",
                        encoding="utf-8")
    seen = {}

    def fake(cfg, log=None, **kwargs):
        seen.update(kwargs)
        return ScrapeResult(rows=[{"a": 1}], pages=1, tier=1, config=dict(cfg))

    monkeypatch.setattr(cli, "scrape", fake)
    cli.main(["run", str(cfg_path), "--no-proxy",
              "--out", str(tmp_path / "o.csv")])
    assert seen["base_dir"] == str(tmp_path)


def test_mcp_proxies_flag_reaches_the_server(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import argparse

    import certainlyscraped.mcp_server as mcp_server

    seen = {}

    class FakeServer:
        def run(self, transport):
            seen["transport"] = transport

    def fake_build(workdir, **kwargs):
        seen.update(kwargs)
        return FakeServer()

    monkeypatch.setattr(mcp_server, "build_server", fake_build)
    path = tmp_path / "p.txt"
    path.write_text("http://u:p@1.2.3.4:8000 # dc\n", encoding="utf-8")
    cli.main(["mcp", "--proxies", str(path), "--workdir", str(tmp_path)])
    assert seen["transport"] == "stdio"
    assert seen["pool"].groups() == ["dc"]


def test_mcp_proxies_flag_reports_a_bad_file(tmp_path, capsys):
    pytest.importorskip("mcp")
    path = tmp_path / "p.txt"
    path.write_text("not-a-proxy\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["mcp", "--proxies", str(path)])
    assert exc.value.code == 1
    assert "line 1" in capsys.readouterr().err


def test_gateway_refuses_a_literal_password_in_the_template():
    """R31: a gateway template is main-config text -- no literal password."""
    cfg = {"proxies": {"gateway": dict(
        GATEWAY, template="http://me-{session}:hunter2@gw.example:7000")}}
    with pytest.raises(ValueError) as exc:
        ProxyPool.from_config(cfg, env={"GATEWAY_PASSWORD": "pw"})
    assert "password_env" in str(exc.value) or "--proxies" in str(exc.value)
    assert "hunter2" not in str(exc.value)


# --- fix round 2: R34 (a challenge parks nothing) --------------------------

def test_a_challenge_parks_nothing_but_a_plain_block_does():
    """R34: blocked-with-challenge is the site's problem, not the proxy's."""
    pool = ProxyPool.from_config({"proxies": ["http://a:1 # a"]}, env={})
    proxy = pool.acquire("h.test")
    pool.report(proxy, "h.test", "blocked", challenge=True)
    assert pool.acquire("h.test") is not None
    pool.report(proxy, "h.test", "blocked")
    assert pool.acquire("h.test") is None
    pool.report(proxy, "h.test", "rate_limited")
    assert pool.acquire("h.test") is None


# --- fix round 2: a gateway host that only lives in the template ----------

def test_gateway_host_only_in_the_template_is_scrubbed():
    """Spec s5's own example: no `host:` key, the address is in the template."""
    cfg = {"proxies": {"gateway": {
        "template": "http://{user}-country-{country}-session-{session}:"
                    "{password}@gw.example:7000",
        "user": "me", "country": "se",
        "password_env": "GATEWAY_PASSWORD", "session": "rotate"}}}
    pool = ProxyPool.from_config(cfg, env={"GATEWAY_PASSWORD": "pw"})
    assert pool.groups() == ["gateway"]
    for text in ("connect to gw.example:7000 failed", "gw.example",
                 "http://gw.example:7000/", "pw"):
        assert "gw.example" not in pool.scrub(text)
        assert "pw" not in pool.scrub(text)


# --- fix round 2: _redact_exc needs the config's directory ----------------

def test_redact_exc_resolves_a_proxies_file_against_the_config(tmp_path,
                                                              monkeypatch):
    (tmp_path / "proxies.txt").write_text(PROXY_URL + "\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # CWD is not the config's directory
    detail = cli._redact_exc(RuntimeError("connect to 10.1.2.3:8000 refused"),
                             {"proxies": {"list": "proxies.txt"}},
                             base_dir=str(tmp_path))
    assert "10.1.2.3" not in detail
    assert "s3cret" not in detail
    assert "***" in detail


# --- fix round 2: R35 proxy precedence ------------------------------------

def test_proxy_precedence_flag_then_env_then_config(tmp_path, monkeypatch):
    """--no-proxy > --proxies FILE > CERTAINLYSCRAPED_PROXIES > config."""
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        "url: https://h.test/1\n"
        "proxies:\n  - 'http://cfg:1 # cfgg'\n"
        "items: {selector: li}\n", encoding="utf-8")
    flag = tmp_path / "p.txt"
    flag.write_text("http://flag:1 # flagg\n", encoding="utf-8")
    seen = {}

    def fake(config, log=None, **kwargs):
        loaded = api.load_config(config)
        pool = ProxyPool.from_config(
            loaded, use_env=kwargs.get("use_env", True),
            proxy_source=kwargs.get("proxy_source", "auto"))
        seen["groups"] = None if pool is None else pool.groups()
        return ScrapeResult(rows=[{"a": 1}], pages=1, tier=1,
                            config=dict(loaded))

    monkeypatch.setattr(cli, "scrape", fake)
    monkeypatch.delenv("CERTAINLYSCRAPED_PROXIES", raising=False)

    def groups(*argv):
        cli.main(["run", str(cfg_path), "--out", str(tmp_path / "o.csv"),
                  *argv])
        return seen["groups"]

    assert groups() == ["cfgg"]                      # config alone
    monkeypatch.setenv("CERTAINLYSCRAPED_PROXIES", "http://env:1 # envg")
    assert groups() == ["envg"]                      # env beats config
    assert groups("--proxies", str(flag)) == ["flagg"]  # flag beats env
    assert groups("--no-proxy") is None              # flag off beats all


# --- fix round 3: mcp --proxies outranks the environment ------------------

def test_mcp_proxies_flag_beats_the_environment(tmp_path, monkeypatch):
    """--proxies FILE is the whole set for `mcp` too (R35), as for `run`."""
    pytest.importorskip("mcp")
    import certainlyscraped.mcp_server as mcp_server

    seen = {}

    class FakeServer:
        def run(self, transport):
            seen["transport"] = transport

    def fake_build(workdir, **kwargs):
        seen.update(kwargs)
        return FakeServer()

    monkeypatch.setattr(mcp_server, "build_server", fake_build)
    path = tmp_path / "p.txt"
    path.write_text("http://flag:1 # flagg\n", encoding="utf-8")
    monkeypatch.setenv("CERTAINLYSCRAPED_PROXIES", "http://env:1 # envg")
    cli.main(["mcp", "--proxies", str(path)])
    assert seen["transport"] == "stdio"
    assert seen["pool"].groups() == ["flagg"]
