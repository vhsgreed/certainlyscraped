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

    def fake_scrape(cfg, *, guard=None, log=None):
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
    def fake_scrape(cfg, *, guard=None, log=None):  # pragma: no cover
        raise AssertionError("must not run")

    cfg = ("url: https://example.com/books\n"
           "since: yesterday.csv\n"
           "items: { selector: 'li' }\n")
    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": cfg})
    assert res["status"] == "error"
    assert "since" in notes(res)


def test_scrape_clamps_tier(tmp_path):
    seen = {}

    def fake_scrape(cfg, *, guard=None, log=None):
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
    def fake_scrape(cfg, *, guard=None, log=None):  # pragma: no cover
        raise AssertionError("must not run")

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "url: /etc/passwd\nitems: {selector: li}\n"})
    assert res["status"] == "error"
    assert "http" in notes(res)


def test_scrape_rejects_non_mapping_config(tmp_path):
    def fake_scrape(cfg, *, guard=None, log=None):  # pragma: no cover
        raise AssertionError("must not run")

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "- just\n- a list\n"})
    assert res["status"] == "error"
    assert res["notes"]


# -- 4. scrape happy path, truncation and the dataset file ------------------

def test_scrape_writes_dataset_and_truncates(tmp_path):
    rows = [{"title": f"book {i}"} for i in range(7)]
    seen = {}

    def fake_scrape(cfg, *, guard=None, log=None):
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

    def fake_scrape(cfg, *, guard=None, log=None):
        return FakeResult(rows)

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "url: https://example.com/x\n"
                             "items: { selector: 'li' }\n"})
    assert res["row_count"] == 4 and len(res["rows"]) == 4
    assert not any("truncated" in n for n in res["notes"])


def test_scrape_caps_max_rows(tmp_path):
    rows = [{"i": i} for i in range(3)]

    def fake_scrape(cfg, *, guard=None, log=None):
        return FakeResult(rows)

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "url: https://example.com/x\n"
                             "items: { selector: 'li' }\n",
                   "max_rows": 10_000})
    assert res["status"] == "complete"
    assert any("500" in n for n in res["notes"])


def test_scrape_caps_max_pages(tmp_path):
    seen = {}

    def fake_scrape(cfg, *, guard=None, log=None):
        seen["cfg"] = cfg
        return FakeResult([{"i": 0}])

    res, _ = call(build_server(tmp_path, _scrape=fake_scrape), "scrape",
                  {"config": "url: https://example.com/x\n"
                             "items: { selector: 'li' }\n"
                             "max_pages: 100000\n"})
    assert any("max_pages" in n for n in res["notes"])
    assert seen["cfg"]["max_pages"] == 1000


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
