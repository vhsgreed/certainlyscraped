"""CLI dispatch tests (no network). Run: python3 -m pytest tests/ -q"""
import sys

sys.path.insert(0, ".")

import json

import pytest

from certainlyscraped import batchprobe, cli, pageprobe
from certainlyscraped.api import ScrapeResult
from certainlyscraped.outcome import PageOutcome


def _capture(monkeypatch, module):
    seen = {}
    monkeypatch.setattr(module, "run", lambda args: seen.setdefault("args", args))
    return seen


def test_batchprobe_dispatch_passes_parsed_args(monkeypatch):
    seen = _capture(monkeypatch, batchprobe)
    cli.main(["batchprobe", "a.txt", "b.txt", "--out", "r.csv", "--workers", "2"])
    a = seen["args"]
    assert (a.files, a.out, a.workers) == (["a.txt", "b.txt"], "r.csv", 2)


def test_pageprobe_dispatch_passes_parsed_args(monkeypatch):
    seen = _capture(monkeypatch, pageprobe)
    cli.main(["pageprobe", "u.txt", "--tiers", "t.csv"])
    a = seen["args"]
    assert (a.files, a.tiers, a.out, a.workers) == (
        ["u.txt"], "t.csv", "pagination-matrix.csv", pageprobe.WORKERS)


def test_subcommand_defaults_match_standalone_parsers(monkeypatch):
    # One argument definition per command: the CLI and the module agree.
    seen = _capture(monkeypatch, batchprobe)
    cli.main(["batchprobe", "a.txt"])
    via_cli = seen.pop("args")
    batchprobe.main(["a.txt"])
    assert vars(via_cli).keys() >= vars(seen["args"]).keys()
    assert via_cli.workers == seen["args"].workers == batchprobe.WORKERS


def test_does_not_touch_sys_argv(monkeypatch):
    _capture(monkeypatch, batchprobe)
    before = list(sys.argv)
    cli.main(["batchprobe", "a.txt"])
    assert sys.argv == before


def test_unknown_subcommand_exits_2():
    with pytest.raises(SystemExit) as e:
        cli.main(["nope"])
    assert e.value.code == 2


def _outcome(url, outcome, status=200, rows=0):
    return PageOutcome(url=url, outcome=outcome, status=status, rows=rows)


def _stub_scrape(monkeypatch, n_rows, outcomes):
    res = ScrapeResult(rows=[{"title": f"t{i}"} for i in range(n_rows)],
                       pages=2, tier=1, config={"output": "csv"},
                       page_report=outcomes)
    monkeypatch.setattr(cli, "scrape", lambda cfg, log=None: res)
    return res


def _incomplete():
    return [_outcome("https://s/a", "ok", 200, 5),
            _outcome("https://s/b", "blocked", 419, 0)]


def test_run_exits_4_on_incomplete(monkeypatch, tmp_path, capsys):
    _stub_scrape(monkeypatch, 5, _incomplete())
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "cfg.yaml", "--out", str(tmp_path / "o.csv"),
                  "--min-rows", "1"])
    assert e.value.code == 4
    err = capsys.readouterr().err
    assert ("[run] incomplete: 1 page(s) blocked/rate-limited/errored; "
            "rows may be missing (exit 4; --accept-partial to accept)") in err


def test_run_accept_partial_exits_0(monkeypatch, tmp_path):
    _stub_scrape(monkeypatch, 5, _incomplete())
    cli.main(["run", "cfg.yaml", "--out", str(tmp_path / "o.csv"),
              "--min-rows", "1", "--accept-partial"])


def test_run_too_few_rows_beats_incomplete(monkeypatch, tmp_path):
    _stub_scrape(monkeypatch, 1, _incomplete())
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "cfg.yaml", "--out", str(tmp_path / "o.csv"),
                  "--min-rows", "5"])
    assert e.value.code == 2


def test_report_jsonl(monkeypatch, tmp_path):
    _stub_scrape(monkeypatch, 5, _incomplete())
    report = tmp_path / "r.jsonl"
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "cfg.yaml", "--out", str(tmp_path / "o.csv"),
                  "--min-rows", "1", "--report", str(report)])
    assert e.value.code == 4
    lines = [json.loads(line) for line in report.read_text().splitlines()]
    assert [line["outcome"] for line in lines] == ["ok", "blocked"]
    assert set(lines[0]) == {"url", "outcome", "status", "rows", "attempts",
                             "steps", "proxy_group", "elapsed_ms", "note"}
