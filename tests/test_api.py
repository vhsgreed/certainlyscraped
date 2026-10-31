"""Python API, stdout output and --since tests (no network).
Run: python3 -m pytest tests/ -q"""
import csv
import io
import json
import sys

sys.path.insert(0, ".")

import pytest

from certainlyscraped import cli, scrape
from certainlyscraped.since import diff_rows

PAGE1 = """<html><body>
<div class="p"><a href="/a">Alpha</a><span class="price">$10</span></div>
<div class="p"><a href="/b">Beta</a><span class="price">$20</span></div>
<a class="next" href="page2.html">next</a></body></html>"""
PAGE2 = """<html><body>
<div class="p"><a href="/c">Gamma</a><span class="price">$30</span></div>
</body></html>"""
CFG = {
    "name": "t", "url": "https://shop.test/page1.html", "tier": 1,
    "rate_limit_sec": 0, "max_pages": 5,
    "paginate": {"selector": "a.next"},
    "items": {"selector": "div.p"},
    "fields": {"title": {"selector": "a"},
               "url": {"selector": "a", "attr": "href"},
               "price": {"selector": "span.price", "type": "int"}},
}


def fake_fetch(url):
    return {"https://shop.test/page1.html": (200, PAGE1),
            "https://shop.test/page2.html": (200, PAGE2)}.get(url, (404, ""))


def test_scrape_api_with_injected_fetcher():
    res = scrape(CFG, fetcher=fake_fetch, log=None)
    assert res.pages == 2
    assert [r["title"] for r in res.rows] == ["Alpha", "Beta", "Gamma"]
    assert res.rows[2]["price"] == 30


def test_scrape_reads_local_files(tmp_path):
    (tmp_path / "page1.html").write_text(PAGE1)
    (tmp_path / "page2.html").write_text(PAGE2)
    cfg = dict(CFG, url=(tmp_path / "page1.html").as_uri(), tier="auto")
    res = scrape(cfg, log=None)
    assert res.pages == 2 and len(res.rows) == 3


def _write_cfg(tmp_path):
    (tmp_path / "page1.html").write_text(PAGE1)
    (tmp_path / "page2.html").write_text(PAGE2)
    cfg = dict(CFG, url=(tmp_path / "page1.html").as_uri(), output="jsonl")
    path = tmp_path / "c.json"   # JSON is valid YAML
    path.write_text(json.dumps(cfg))
    return str(path)


def test_run_to_stdout(tmp_path, capsys):
    cli.main(["run", _write_cfg(tmp_path), "--out", "-"])
    out = capsys.readouterr().out
    rows = [json.loads(line) for line in out.splitlines()]
    assert [r["title"] for r in rows] == ["Alpha", "Beta", "Gamma"]


def test_run_since_with_rolling_snapshot(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    state = str(tmp_path / "state.csv")
    delta = str(tmp_path / "delta.jsonl")
    argv = ["run", cfg, "--out", delta, "--since", state,
            "--snapshot", state, "--key", "url"]
    cli.main(argv)
    assert len(open(delta).read().splitlines()) == 3   # first run: all new

    # Beta's price changes, a new item appears.
    (tmp_path / "page2.html").write_text(PAGE2.replace(
        "</body>", '<div class="p"><a href="/d">Delta</a>'
                   '<span class="price">$40</span></div></body>'))
    (tmp_path / "page1.html").write_text(PAGE1.replace("$20", "$25"))
    cli.main(argv)
    rows = [json.loads(line) for line in open(delta)]
    assert {(r["title"], r["_change"]) for r in rows} == {
        ("Beta", "changed"), ("Delta", "new")}
    with open(state) as f:
        assert len(list(csv.DictReader(f))) == 4


def test_since_empty_delta_still_exits_zero(tmp_path):
    cfg = _write_cfg(tmp_path)
    state = str(tmp_path / "state.csv")
    argv = ["run", cfg, "--out", str(tmp_path / "d.csv"), "--since", state,
            "--snapshot", state]
    cli.main(argv)
    cli.main(argv)  # nothing changed: 0 rows out, but scrape verified OK


def test_broken_selector_still_fails_with_since(tmp_path):
    cfg = _write_cfg(tmp_path)
    data = json.loads(open(cfg).read())
    data["items"]["selector"] = "div.nope"
    open(cfg, "w").write(json.dumps(data))
    with pytest.raises(SystemExit) as e:
        cli.main(["run", cfg, "--out", str(tmp_path / "d.csv"),
                  "--since", str(tmp_path / "s.csv")])
    assert e.value.code == 2


def test_missing_config_exits_1(tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", str(tmp_path / "nope.yaml")])
    assert e.value.code == 1


def test_diff_rows_without_key_compares_whole_row():
    prev = [{"a": "1", "b": "2"}]
    cur = [{"a": 1, "b": 2}, {"a": 1, "b": 3}]
    out, counts = diff_rows(cur, prev)
    assert out == [{"a": 1, "b": 3, "_change": "new"}]
    assert counts == {"new": 1, "changed": 0, "unchanged": 1}


def test_fetch_error_exits_nonzero_without_traceback(tmp_path, capsys):
    # R28: the links path turns the fetch exception into a page error
    # outcome (like the parallel path), so the run reports zero rows and the
    # dataset gate decides the exit code -- never a traceback, never exit 1
    # from an uncaught ProxyError.
    cfg = dict(CFG, url="http://127.0.0.1:9/", tier=1)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(SystemExit) as e:
        cli.main(["run", str(path), "--out", str(tmp_path / "o.csv")])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "1 error" in err


def test_scrape_log_reports_error_status_mid_pagination():
    def blocked(url):
        if url.endswith("page2.html"):
            return 419, "Sorry"
        return fake_fetch(url)
    log = io.StringIO()
    res = scrape(CFG, fetcher=blocked, log=log)
    assert res.pages == 1
    assert "[paginate] stopped: HTTP 419 on https://shop.test/page2.html" in log.getvalue()


def _rate_limited(url):
    if url.endswith("page2.html"):
        return 419, "Sorry"
    return fake_fetch(url)


def test_page_report_marks_419_rate_limited():
    res = scrape(CFG, fetcher=_rate_limited, log=None)
    assert [o.outcome for o in res.page_report] == ["ok", "rate_limited"]
    assert res.page_report[1].status == 419
    assert res.page_report[0].rows == 2
    assert res.status() == "incomplete"


def test_done_line_counts_outcomes():
    log = io.StringIO()
    scrape(CFG, fetcher=_rate_limited, log=log)
    text = log.getvalue()
    assert "[done] 2 pages: 1 ok, 1 rate_limited (HTTP 419 on /page2.html)" in text
    assert "[rows] 2 unique items" in text
