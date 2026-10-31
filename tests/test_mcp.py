"""The MCP server: tools, the private-address guard, and the config allow-list.

No network: every tool test either takes a refusal path (which must be decided
before any request) or replaces the scraper/page loader through the injectable
seams ``build_server`` exposes. One test starts the real stdio subprocess and
does a tools/list round trip, which is also the proof that our own code wrote
nothing to stdout.
Run: python3 -m pytest tests/ -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402

from certainlyscraped.mcp_server import (  # noqa: E402
    ALLOWED_CONFIG_KEYS,
    build_server,
)

FIX = Path(__file__).parent / "fixtures"
BOOKS = FIX / "books.html"
TOOLS = {"probe", "init", "try_selector", "scrape"}


def run(coro):
    return asyncio.run(coro)


def call(server, name, args):
    """Call one tool in-process; return (payload, raw result)."""
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args)
    result = run(go())
    return json.loads(result.content[0].text), result


def notes(payload):
    return " ".join(payload.get("notes") or [])


class FakeResult:
    """Stands in for api.ScrapeResult: only what the tool is allowed to read."""

    def __init__(self, rows, pages=()):
        self.rows = rows
        self.page_report = list(pages)
        self.tier = 1

    def status(self, min_rows=1):
        if not self.rows:
            return "too_few_rows"
        return "complete" if len(self.rows) >= min_rows else "too_few_rows"


def fake_load_page(url, tier="auto", log=sys.stderr, guard=None):
    return url, BOOKS.read_text(encoding="utf-8"), 1


# -- 1. the surface an agent sees -------------------------------------------

def test_tools_listed(tmp_path):
    async def go():
        async with Client(build_server(tmp_path)) as c:
            return await c.list_tools()

    tools = {t.name: t for t in run(go()).tools}
    assert set(tools) == TOOLS
    for name, tool in tools.items():
        assert tool.description and tool.description.strip(), name
        assert tool.input_schema["type"] == "object"


# -- 2. file:// is not a way in ---------------------------------------------

def test_scrape_refuses_file_url(tmp_path):
    (tmp_path / "page.html").write_text(BOOKS.read_text(encoding="utf-8"),
                                        encoding="utf-8")
    cfg = (f"url: file://{tmp_path / 'page.html'}\n"
           "items: { selector: 'article.product_pod' }\n")
    res, _ = call(build_server(tmp_path, allow_private=True), "scrape",
                  {"config": cfg})
    assert res["status"] == "error"
    assert "file://" in notes(res) or "refused" in notes(res)


def test_probe_refuses_file_url(tmp_path):
    res, _ = call(build_server(tmp_path, allow_private=True), "probe",
                  {"url": "file:///etc/passwd"})
    assert res["status"] == "error"
    assert "http" in res["note"]


# -- 3. the config allow-list and the tier clamp ----------------------------

def test_scrape_rejects_refused_key(tmp_path):
    seen = {}

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        seen["cfg"] = cfg
        return FakeResult([{"a": 1}])

    cfg = ("url: https://example.com/books\n"
           "proxy: http://127.0.0.1:8080\n"
           "items: { selector: 'li' }\n")
    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": cfg})
    assert res["status"] == "error"
    assert "proxy" in notes(res)
    assert "cfg" not in seen  # never reached the scraper


def test_scrape_rejects_unknown_key(tmp_path):
    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):  # pragma: no cover
        raise RuntimeError("must not run")

    cfg = ("url: https://example.com/books\n"
           "since: yesterday.csv\n"
           "items: { selector: 'li' }\n")
    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": cfg})
    assert res["status"] == "error"
    assert "since" in notes(res)


def test_scrape_clamps_tier(tmp_path):
    seen = {}

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        seen["cfg"] = cfg
        return FakeResult([{"a": 1}])

    cfg = ("url: https://example.com/books\n"
           "tier: 3\n"
           "items: { selector: 'li' }\n")
    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": cfg})
    assert "max_tier clamped to 2" in notes(res)
    assert seen["cfg"]["tier"] == 2
    assert res["status"] == "complete"


def test_scrape_rejects_non_http_url(tmp_path):
    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):  # pragma: no cover
        raise RuntimeError("must not run")

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "url: /etc/passwd\nitems: {selector: li}\n"})
    assert res["status"] == "error"
    assert "http" in notes(res)


def test_scrape_rejects_non_mapping_config(tmp_path):
    seen = []

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):  # pragma: no cover
        seen.append(cfg)
        raise RuntimeError("must not run")

    res, raw = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                    {"config": "- just\n- a list\n"})
    assert res["status"] == "error"
    assert "mapping" in notes(res)
    assert not raw.is_error
    assert seen == []


# -- 4. scrape happy path, truncation and the dataset file ------------------

def test_scrape_writes_dataset_and_truncates(tmp_path):
    rows = [{"title": f"book {i}"} for i in range(7)]
    seen = {}

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        seen["log"] = log
        return FakeResult(rows)

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "url: https://example.com/books\n"
                             "items: { selector: 'li' }\n",
                   "max_rows": 3})
    assert res["status"] == "complete"
    assert res["row_count"] == 7
    assert res["rows"] == rows[:3]
    assert any("truncated" in n for n in res["notes"])
    assert seen["log"] is None  # silence: stdout belongs to the protocol

    path = Path(res["dataset_path"])
    assert path.parent == tmp_path / ".certainlyscraped"
    assert path.suffix == ".jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 7
    assert json.loads(lines[0]) == rows[0]


def test_scrape_does_not_truncate_without_need(tmp_path):
    rows = [{"i": i} for i in range(4)]

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        return FakeResult(rows)

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "url: https://example.com/x\n"
                             "items: { selector: 'li' }\n"})
    assert res["row_count"] == 4 and len(res["rows"]) == 4
    assert not any("truncated" in n for n in res["notes"])


def test_scrape_caps_max_rows(tmp_path):
    rows = [{"i": i} for i in range(3)]

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        return FakeResult(rows)

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "url: https://example.com/x\n"
                             "items: { selector: 'li' }\n",
                   "max_rows": 10_000})
    assert res["status"] == "complete"
    assert any("500" in n for n in res["notes"])


def test_scrape_caps_max_pages(tmp_path):
    seen = {}

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        seen["cfg"] = cfg
        return FakeResult([{"i": 0}])

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "url: https://example.com/x\n"
                             "items: { selector: 'li' }\n"
                             "max_pages: 100000\n"})
    assert any("max_pages" in n for n in res["notes"])
    # 100 is follow_links' own ceiling (min(cfg max_pages, 100)).
    assert seen["cfg"]["max_pages"] == 100


def test_scrape_refuses_dataset_escaping_workdir(tmp_path):
    (tmp_path / ".certainlyscraped").symlink_to(tmp_path.parent, target_is_directory=True)
    res, _ = call(build_server(tmp_path,
                               _scrape=lambda cfg, **kw: FakeResult([{"i": 0}])),
                  "scrape", {"config": "url: https://example.com/x\n"
                                       "items: { selector: 'li' }\n"})
    assert res["status"] == "error"
    assert "outside the working directory" in notes(res)


# -- 5. the guard -----------------------------------------------------------

def test_probe_private_refused(tmp_path):
    res, _ = call(build_server(tmp_path), "probe",
                  {"url": "http://127.0.0.1:9/"})
    assert res["status"] == "error"
    assert res["private_refused"] is True
    assert "private address" in res["note"]


def test_scrape_private_refused(tmp_path):
    res, _ = call(build_server(tmp_path), "scrape",
                  {"config": "url: http://127.0.0.1:9/\n"
                             "items: { selector: 'li' }\n"})
    assert res["status"] == "error"
    assert "private address" in notes(res)


# -- 6. try_selector and init ----------------------------------------------

def test_try_selector_matches(tmp_path):
    server = build_server(tmp_path, _load_page=fake_load_page)
    res, _ = call(server, "try_selector",
                  {"url": "https://example.com/books",
                   "selector": "article.product_pod h3 a"})
    assert res["status"] == "ok"
    assert res["matches"] == 6
    assert len(res["values"]) == 6
    assert res["tags"]


def test_try_selector_within(tmp_path):
    server = build_server(tmp_path, _load_page=fake_load_page)
    res, _ = call(server, "try_selector",
                  {"url": "https://example.com/books",
                   "selector": "h3 a",
                   "within": "article.product_pod"})
    assert res["status"] == "ok"
    assert res["matches"] == 6


def test_try_selector_nothing_found(tmp_path):
    server = build_server(tmp_path, _load_page=fake_load_page)
    res, _ = call(server, "try_selector",
                  {"url": "https://example.com/books",
                   "selector": "div.no-such-thing"})
    assert res["status"] == "nothing"
    assert res["matches"] == 0
    assert res["hints"]


def test_try_selector_invalid_selector_does_not_raise(tmp_path):
    server = build_server(tmp_path, _load_page=fake_load_page)
    res, _ = call(server, "try_selector",
                  {"url": "https://example.com/books",
                   "selector": "div[["})
    assert res["status"] == "error"
    assert res["warnings"] or res["hints"]


def test_init_returns_config_text(tmp_path):
    server = build_server(tmp_path, _load_page=fake_load_page)
    res, _ = call(server, "init", {"url": "https://example.com/books"})
    assert res["status"] == "ok"
    assert "items:" in res["config"] and "fields:" in res["config"]
    assert len(res["rows_preview"]) == 3
    assert res["fields"] and res["items"]

    import yaml
    cfg = yaml.safe_load(res["config"])
    assert isinstance(cfg, dict)
    assert cfg["url"] == "https://example.com/books"
    # What init hands back must be usable by scrape as-is.
    assert set(cfg) <= ALLOWED_CONFIG_KEYS


def test_init_on_blank_page_does_not_raise(tmp_path):
    server = build_server(
        tmp_path, _load_page=lambda u, **kw: (u, "<html><body>hi</body></html>", 1))
    res, _ = call(server, "init", {"url": "https://example.com/empty"})
    assert res["status"] == "error"
    assert res["note"]


# -- 7. the real stdio server ----------------------------------------------

def test_cli_mcp_without_the_extra(monkeypatch, capsys):
    from certainlyscraped import cli

    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["mcp"])
    assert exc.value.code == 1
    assert 'pip install "certainlyscraped[mcp]"' in capsys.readouterr().err


def test_stdio_server_roundtrip(tmp_path):
    root = str(Path(__file__).resolve().parent.parent)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "certainlyscraped", "mcp", "--allow-private",
              "--workdir", str(tmp_path)],
        env={**os.environ, "PYTHONPATH": root},
    )

    async def go():
        async with Client(params) as c:
            tools = await c.list_tools()
            res = await c.call_tool("probe", {"url": "http://127.0.0.1:9/"})
            return tools, res

    tools, res = run(go())
    assert {t.name for t in tools.tools} == TOOLS
    # Valid JSON text on the wire: our code printed nothing to stdout, so the
    # JSON-RPC framing stayed intact and the payload is parseable.
    payload = json.loads(res.content[0].text)
    assert payload["status"] in {"error", "blocked"}, payload
    assert (tmp_path / ".certainlyscraped").is_dir() is False


# -- 8. numeric config is bounded before the first fetch (R15) --------------

def _recording_scraper(seen):
    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        seen["cfg"] = dict(cfg)
        return FakeResult([{"i": 0}])
    return fake_scrape


def _scrape_with(tmp_path, want_cfg: str, **args):
    seen = {}
    start = time.monotonic()
    res, raw = call(build_server(tmp_path, _scrape=_recording_scraper(seen)),
                    "scrape", {"config": want_cfg, **args})
    elapsed = time.monotonic() - start
    return res, raw, seen.get("cfg", {}), elapsed


def test_rate_limit_at_the_cap_returns_quickly(tmp_path):
    res, raw, cfg, elapsed = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "rate_limit_sec: 30\nmax_pages: 2\n")
    assert not raw.is_error
    assert elapsed < 2, elapsed
    assert res["status"] == "complete"
    assert cfg["rate_limit_sec"] == 30.0
    assert not any("rate_limit_sec" in n for n in res["notes"])


def test_rate_limit_above_the_cap_is_clamped(tmp_path):
    res, _, cfg, elapsed = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "rate_limit_sec: 5000\n")
    assert elapsed < 2, elapsed
    assert res["status"] == "complete"
    assert cfg["rate_limit_sec"] == 30.0
    assert any("rate_limit_sec" in n and "30" in n for n in res["notes"])


@pytest.mark.parametrize("bad", ["abc", -5, ".nan", ".inf"])
def test_rate_limit_bad_value_falls_back_with_a_note(tmp_path, bad):
    res, raw, cfg, elapsed = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  f"rate_limit_sec: {bad}\n")
    assert not raw.is_error
    assert elapsed < 2, elapsed
    assert res["status"] == "complete"          # rows were not thrown away
    assert cfg["rate_limit_sec"] == 2.0         # the library's own default
    assert any("rate_limit_sec" in n for n in res["notes"])


def test_max_pages_cap_states_the_library_ceiling(tmp_path):
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "max_pages: 5000\n")
    assert cfg["max_pages"] == 100
    assert any("max_pages" in n and "100" in n for n in res["notes"])


def test_timeout_bad_value_falls_back_with_a_note(tmp_path):
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "timeout: 'x'\n")
    assert res["status"] == "complete"
    assert cfg["timeout"] == 30.0
    assert any("timeout" in n for n in res["notes"])


def test_timeout_is_clamped_into_range(tmp_path):
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "timeout: 100000\n")
    assert cfg["timeout"] == 120.0
    assert any("timeout" in n for n in res["notes"])
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "timeout: -3\n")
    assert cfg["timeout"] == 1.0
    assert any("timeout" in n for n in res["notes"])


@pytest.mark.parametrize("key", ["items", "fields", "paginate"])
def test_non_mapping_blocks_are_refused_before_any_fetch(tmp_path, key):
    def boom(cfg, **kw):  # pragma: no cover
        raise RuntimeError("must not run")

    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": "url: https://example.com/x\n"
                               "items: { selector: 'li' }\n"
                               f"{key}: 'oops'\n"})
    assert not raw.is_error
    assert res["status"] == "error"
    assert any(key in n for n in res["notes"])


def test_paginate_must_be_a_mapping_before_any_fetch(tmp_path):
    def boom(cfg, **kw):  # pragma: no cover
        raise RuntimeError("must not run")

    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": "url: https://example.com/x\n"
                               "items: { selector: 'li' }\n"
                               "paginate: 'next'\n"})
    assert not raw.is_error
    assert res["status"] == "error"
    assert any("paginate" in n for n in res["notes"])


# -- 9. a mistyped argument is still a status dict (R16) --------------------

def test_scrape_non_string_config_is_a_status_dict(tmp_path):
    def boom(cfg, **kw):  # pragma: no cover
        raise RuntimeError("must not run")

    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": 123})
    assert not raw.is_error
    assert res["status"] == "error"
    assert res["notes"] and "config" in notes(res)


def test_probe_non_string_url_is_a_status_dict(tmp_path):
    res, raw = call(build_server(tmp_path), "probe", {"url": 127})
    assert not raw.is_error
    assert res["status"] == "error"
    assert "url must be a string" in res["note"]


def test_init_non_string_url_is_a_status_dict(tmp_path):
    res, raw = call(build_server(tmp_path), "init", {"url": 12.5})
    assert not raw.is_error
    assert res["status"] == "error"
    assert "url must be a string" in notes(res)


def test_try_selector_loose_arguments_are_a_status_dict(tmp_path):
    res, raw = call(build_server(tmp_path), "try_selector",
                    {"url": 127, "selector": 5})
    assert not raw.is_error
    assert res["status"] == "error"
    assert res["hints"]


@pytest.mark.parametrize("name", ["probe", "init", "try_selector", "scrape"])
def test_missing_arguments_are_a_status_dict(tmp_path, name):
    """Even a call with no arguments is a result the agent can read."""
    res, raw = call(build_server(tmp_path), name, {})
    assert not raw.is_error
    assert res["status"] == "error"


def test_scrape_non_numeric_max_rows_uses_the_default(tmp_path):
    rows = [{"i": i} for i in range(60)]
    seen = {}

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        seen["cfg"] = cfg
        return FakeResult(rows)

    res, raw = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                    {"config": "url: https://example.com/x\n"
                               "items: { selector: 'li' }\n",
                     "max_rows": "abc"})
    assert not raw.is_error
    assert res["status"] == "complete"
    assert len(res["rows"]) == 50            # DEFAULT_MAX_ROWS
    assert any("max_rows" in n for n in res["notes"])


# -- 10. pages, urls and the ladder knobs (R17/R21) -------------------------

BOOKS_CFG = ("items: { selector: 'article.product_pod' }\n"
             "fields:\n"
             "  title: { selector: 'h3 a', attr: title }\n")


def _api_with_pages(html, seen: dict):
    """A scrape implementation that runs the real API over a fake network.

    With ``html=None`` each URL gets one distinct item, so dedupe (which is
    by row value) cannot turn later pages into ``empty``.
    """
    from certainlyscraped.api import scrape as api_scrape

    def fetcher(url):
        seen.setdefault("urls", []).append(url)
        if html is None:
            return 200, (f"<article class='product_pod'><h3>"
                         f"<a title='{url}' href='{url}'>x</a></h3></article>")
        return 200, html

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        seen["cfg"] = dict(cfg)
        return api_scrape(cfg, fetcher=fetcher, log=None)

    return fake_scrape


def _scrape_with_config(tmp_path, text, html=None):
    seen = {}
    res, raw = call(build_server(tmp_path,
                                 _scrape=_api_with_pages(html, seen)),
                    "scrape", {"config": text})
    return res, raw, seen


def test_pages_config_scrapes_without_a_top_level_url(tmp_path):
    cfg = ("pages:\n"
           "  template: 'https://example.com/news?p={n}'\n"
           "  from: 1\n"
           "  to: 3\n" + BOOKS_CFG)
    res, raw, seen = _scrape_with_config(tmp_path, cfg)
    assert not raw.is_error
    assert res["status"] == "complete"
    assert len(res["pages"]) == 3
    assert {p["outcome"] for p in res["pages"]} == {"ok"}
    assert seen["urls"] == [f"https://example.com/news?p={n}"
                            for n in (1, 2, 3)]
    assert res["row_count"] > 0


def test_urls_config_scrapes_without_a_top_level_url(tmp_path):
    cfg = ("urls:\n"
           "  - 'https://example.com/a'\n"
           "  - 'https://example.com/b'\n" + BOOKS_CFG)
    res, raw, seen = _scrape_with_config(tmp_path, cfg)
    assert not raw.is_error
    assert res["status"] == "complete"
    assert [p["url"] for p in res["pages"]] == ["https://example.com/a",
                                                "https://example.com/b"]
    assert seen["urls"] == ["https://example.com/a", "https://example.com/b"]


def test_pages_range_is_capped_with_a_note(tmp_path):
    res, raw, seen = _scrape_with_config(
        tmp_path, "pages:\n"
                  "  template: 'https://example.com/p={n}'\n"
                  "  from: 1\n"
                  "  to: 500\n" + BOOKS_CFG, html="<html></html>")
    assert not raw.is_error
    assert seen["cfg"]["pages"]["to"] == 100
    assert any("pages" in n and "100" in n for n in res["notes"])


@pytest.mark.parametrize("pages", [
    "'oops'",
    "{template: 'https://example.com/p', from: 1, to: 2}",
    "{template: 'https://example.com/p={n}', from: 'x', to: 2}",
    "{template: 'https://example.com/p={n}', from: 5, to: 2}",
    "{template: 'example.com/p={n}', from: 1, to: 2}",
    # A scheme is not enough: without a host the guard dies after the
    # config was accepted.
    "{template: 'http:///p={n}', from: 1, to: 2}",
    # A host made only of the placeholder is still no host at all.
    "{template: 'http://{n}', from: 1, to: 2}",
    "{template: 'http://{n}/x', from: 1, to: 2}",
    "{template: 'http://{host}/p={n}', from: 1, to: 2}",
])
def test_malformed_pages_is_a_status_dict(tmp_path, pages):
    def boom(cfg, **kw):  # pragma: no cover
        raise RuntimeError("must not run")

    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": f"pages: {pages}\n" + BOOKS_CFG})
    assert not raw.is_error
    assert res["status"] == "error"
    assert any("pages" in n for n in res["notes"])


def test_pages_template_with_a_real_host_and_placeholder_path_passes(tmp_path):
    res, raw, seen = _scrape_with_config(
        tmp_path, "pages:\n"
                  "  template: 'http://example.com/{n}'\n"
                  "  from: 1\n"
                  "  to: 2\n" + BOOKS_CFG, html="<html></html>")
    assert not raw.is_error
    assert seen["cfg"]["pages"]["template"] == "http://example.com/{n}"


def test_urls_are_capped_with_a_note(tmp_path):
    urls = [f"https://example.com/{i}" for i in range(150)]
    seen = {}

    def fake_scrape(cfg, *, guard=None, log=None, **kwargs):
        seen["cfg"] = dict(cfg)
        return FakeResult([{"i": 0}])

    res, raw = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                    {"config": "urls:\n" + "".join(f"  - '{u}'\n" for u in urls)
                               + BOOKS_CFG})
    assert not raw.is_error
    assert res["status"] == "complete"
    assert len(seen["cfg"]["urls"]) == 100
    assert any("urls" in n and "100" in n for n in res["notes"])


@pytest.mark.parametrize("entry,needle", [
    ("/relative/page", "urls[1]"),
    ("ftp://example.com/x", "urls[1]"),
    ("", "urls[1]"),
    ("123", "urls[1]"),
    ("http://", "urls[1]"),
    ("http:///no-host", "urls[1]"),
])
def test_urls_relative_or_non_string_entry_is_refused(tmp_path, entry, needle):
    def boom(cfg, **kw):  # pragma: no cover
        raise RuntimeError("must not run")

    raw_yaml = ("urls:\n"
                "  - 'https://example.com/a'\n"
                f"  - {entry!r}\n" if not str(entry).isdigit()
                else "urls:\n"
                     "  - 'https://example.com/a'\n"
                     f"  - {entry}\n")
    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": raw_yaml + BOOKS_CFG})
    assert not raw.is_error
    assert res["status"] == "error"
    assert any(needle in n for n in res["notes"])


def test_urls_empty_is_refused(tmp_path):
    def boom(cfg, **kw):  # pragma: no cover
        raise RuntimeError("must not run")

    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": "urls: []\n" + BOOKS_CFG})
    assert not raw.is_error
    assert res["status"] == "error"
    assert any("urls" in n for n in res["notes"])


def test_two_page_sources_are_refused_before_any_fetch(tmp_path):
    def boom(cfg, **kw):  # pragma: no cover
        raise RuntimeError("must not run")

    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": "pages: {template: 'https://e.com/{n}', to: 2}\n"
                               "urls: ['https://e.com/a']\n" + BOOKS_CFG})
    assert not raw.is_error
    assert res["status"] == "error"
    assert any("page source" in n for n in res["notes"])


def test_concurrency_is_validated_and_clamped(tmp_path):
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "concurrency: 4\n")
    assert res["status"] == "complete"
    assert cfg["concurrency"] == 4
    assert not any("concurrency" in n for n in res["notes"])

    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "concurrency: 500\n")
    assert cfg["concurrency"] == 16
    assert any("concurrency" in n for n in res["notes"])

    res, raw, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "concurrency: 'lots'\n")
    assert not raw.is_error
    assert res["status"] == "complete"
    assert cfg["concurrency"] == 1
    assert any("concurrency" in n for n in res["notes"])

    # A float used to be truncated silently; the note must name the value.
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "concurrency: 3.7\n")
    assert cfg["concurrency"] == 3
    assert any("3.7" in n and "truncat" in n for n in res["notes"])


def test_max_tier_is_capped_at_two_with_a_note(tmp_path):
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "max_tier: 3\n")
    assert res["status"] == "complete"
    assert cfg["max_tier"] == 2
    assert any("max_tier" in n for n in res["notes"])

    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "max_tier: 1\n")
    assert cfg["max_tier"] == 1
    assert not any("max_tier" in n for n in res["notes"])


def test_max_retries_is_bounded(tmp_path):
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "max_retries: 9\n")
    assert cfg["max_retries"] == 3
    assert any("max_retries" in n for n in res["notes"])

    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "max_retries: 0\n")
    assert cfg["max_retries"] == 0
    assert not any("max_retries" in n for n in res["notes"])

    res, raw, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "max_retries: 'many'\n")
    assert not raw.is_error
    assert res["status"] == "complete"
    assert cfg["max_retries"] == 3
    assert any("max_retries" in n for n in res["notes"])


def test_cooldown_sec_is_capped_below_the_blocking_sleep_limit(tmp_path):
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "cooldown_sec: 300\n")
    assert cfg["cooldown_sec"] == 60.0
    assert any("cooldown_sec" in n and "60" in n for n in res["notes"])

    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "cooldown_sec: 2.5\n")
    assert cfg["cooldown_sec"] == 2.5
    assert not any("cooldown_sec" in n for n in res["notes"])


def test_per_proxy_rate_sec_is_bounded(tmp_path):
    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "per_proxy_rate_sec: 500\n")
    assert cfg["per_proxy_rate_sec"] == 30.0
    assert any("per_proxy_rate_sec" in n for n in res["notes"])

    res, _, cfg, _ = _scrape_with(
        tmp_path, "url: https://example.com/x\nitems: { selector: 'li' }\n"
                  "per_proxy_rate_sec: 0.5\n")
    assert cfg["per_proxy_rate_sec"] == 0.5
    assert not any("per_proxy_rate_sec" in n for n in res["notes"])


@pytest.mark.parametrize("key", ["proxy", "proxies", "cookies"])
def test_server_side_keys_stay_refused_alongside_pages(tmp_path, key):
    def boom(cfg, **kw):  # pragma: no cover
        raise RuntimeError("must not run")

    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": "pages: {template: 'https://e.com/{n}', to: 2}\n"
                               f"{key}: 'server-side'\n" + BOOKS_CFG})
    assert not raw.is_error
    assert res["status"] == "error"
    assert any(key in n for n in res["notes"])


# -- 11. fix round A: the server's own pool and the env (C1/R40, I1/R41) ----

ITEM_CFG = ("url: https://example.com/list\ntier: 1\n"
            "items: {selector: div.item}\n"
            "fields: {title: {selector: a}}\n")
ITEM_HTML = ("<html><body><div class='item'><a href='/x'>x</a></div>"
             "</body></html>")


def _fake_api_fetch(monkeypatch, fn):
    import certainlyscraped.api as api_mod
    monkeypatch.setattr(api_mod, "_fetch", fn)


def test_mcp_scrape_never_reads_the_env_proxy(monkeypatch, tmp_path):
    """I1/R41: `mcp` without --proxies has no pool, even with the env set."""
    calls = []

    def fake_fetch(url, tier, **kwargs):
        calls.append((tier, kwargs.get("proxy")))
        return 200, ITEM_HTML

    _fake_api_fetch(monkeypatch, fake_fetch)
    monkeypatch.setenv("CERTAINLYSCRAPED_PROXIES", "http://127.0.0.1:18082")
    res, raw = call(build_server(tmp_path), "scrape", {"config": ITEM_CFG})
    assert not raw.is_error
    assert [s for page in res["pages"] for s in page["steps"]] == ["t1"]
    assert calls and all(proxy is None for _, proxy in calls)


def test_mcp_scrape_passes_use_env_false(tmp_path):
    seen = {}

    def fake_scrape(cfg, **kwargs):
        seen.update(kwargs)
        return FakeResult([{"i": 0}])

    call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
         {"config": ITEM_CFG})
    assert seen.get("use_env") is False


def test_mcp_probe_leases_a_proxy_from_the_server_pool(tmp_path):
    from certainlyscraped.proxypool import ProxyPool

    pool = ProxyPool.from_config(
        {"proxies": ["http://10.1.2.3:8000 # p"]}, env={})
    seen = {}

    class Result:
        tier = 1
        confident = True
        note = "static page"
        body_size = 10
        markers = []
        embedded = []

    def fake_probe(url, guard=None, proxy=None):
        seen["proxy"] = proxy
        return Result()

    res, _ = call(build_server(tmp_path, pool=pool, _probe=fake_probe),
                  "probe", {"url": "https://example.com/x"})
    assert res["status"] == "ok"
    assert seen["proxy"] is not None
    assert seen["proxy"].url == "http://10.1.2.3:8000"


class _EmptyPool:
    def acquire(self, host):
        return None


@pytest.mark.parametrize("name,args", [
    ("probe", {"url": "https://example.com/x"}),
    ("init", {"url": "https://example.com/x"}),
    ("try_selector", {"url": "https://example.com/x", "selector": "a"}),
])
def test_mcp_page_tools_without_a_lease_are_blocked(tmp_path, name, args):
    """R40: a pool is never bypassed, and no lease means no request."""
    calls = []

    def tool_impl(*a, **kw):
        calls.append(1)  # pragma: no cover - must never run
        raise RuntimeError("fetched without a lease")

    server = build_server(tmp_path, pool=_EmptyPool(),
                          _probe=tool_impl, _load_page=tool_impl)
    res, raw = call(server, name, args)
    assert not raw.is_error
    assert res["status"] == "blocked"
    note = res.get("note") or notes(res)
    assert "no proxy available for example.com" in note
    assert calls == []


# -- 12. I3/R43: a guard error keeps its rung under MCP --------------------

def _scrape_raising(monkeypatch, tmp_path, exc):
    def fake_fetch(url, tier, **kwargs):
        raise exc

    _fake_api_fetch(monkeypatch, fake_fetch)
    return call(build_server(tmp_path), "scrape", {"config": ITEM_CFG})


def test_mcp_private_address_refusal_is_terminal_but_keeps_the_rung(
        monkeypatch, tmp_path):
    from certainlyscraped.netguard import PrivateAddressError

    res, raw = _scrape_raising(
        monkeypatch, tmp_path, PrivateAddressError("private address: 10.0.0.1"))
    assert not raw.is_error
    page = res["pages"][0]
    assert page["attempts"] == 1 == len(page["steps"])
    assert page["steps"] == ["t1"]
    assert "PrivateAddressError" in page["note"]


def test_mcp_cannot_resolve_is_an_ordinary_error_with_steps(
        monkeypatch, tmp_path):
    res, raw = _scrape_raising(
        monkeypatch, tmp_path, ValueError("cannot resolve no-such-host.invalid"))
    assert not raw.is_error
    page = res["pages"][0]
    assert page["steps"] == ["t1", "t1", "t2", "t2"]
    assert page["attempts"] == 4 == len(page["steps"])
    assert "cannot resolve" in page["note"]


# -- 13. mutation 12: an ordinary exception never escapes a tool ------------

def test_scrape_internal_exception_is_a_status_dict(tmp_path):
    def boom(cfg, **kw):
        raise RuntimeError("kaboom")

    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": ITEM_CFG})
    assert not raw.is_error
    assert res["status"] == "error"
    assert "RuntimeError: kaboom" in notes(res)


# -- 14. cheap fixes: min_rows note, within type, allow-list ----------------

def test_min_rows_coercion_note_is_kept(tmp_path):
    res, raw, cfg, _ = _scrape_with(tmp_path, ITEM_CFG, min_rows="abc")
    assert not raw.is_error
    assert res["status"] == "complete"
    assert any("min_rows" in n for n in res["notes"])


def test_try_selector_with_within_not_a_string_is_readable(tmp_path):
    server = build_server(tmp_path, _load_page=fake_load_page)
    res, raw = call(server, "try_selector",
                    {"url": "https://example.com/books",
                     "selector": "h3 a", "within": 5})
    assert not raw.is_error
    assert res["status"] == "error"
    assert any("within" in hint and "string" in hint
               for hint in res["hints"])


@pytest.mark.parametrize("key", ["json", "headers"])
def test_dead_config_keys_are_now_unknown(tmp_path, key):
    """R46: neither key is read, so neither may be accepted."""
    def boom(cfg, **kw):  # pragma: no cover
        raise RuntimeError("must not run")

    res, raw = call(build_server(tmp_path, _scrape=boom), "scrape",
                    {"config": ITEM_CFG + f"{key}: 'oops'\n"})
    assert not raw.is_error
    assert res["status"] == "error"
    assert any(f"unknown config key '{key}'" in n for n in res["notes"])


def test_scrape_description_lists_neither_json_nor_headers(tmp_path):
    async def go():
        async with Client(build_server(tmp_path)) as c:
            return await c.list_tools()

    tools = {t.name: t for t in run(go()).tools}
    text = tools["scrape"].description or ""
    assert "headers" not in text
    assert "json," not in text
    assert "json" not in ALLOWED_CONFIG_KEYS
    assert "headers" not in ALLOWED_CONFIG_KEYS

