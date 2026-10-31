"""CLI: init, try, fetch, probe, batchprobe, pageprobe, run, verify."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import yaml

from . import assist, batchprobe, pageprobe
from .api import load_config, scrape
from .classify import probe
from .fetch import redact
from .outcome import INCOMPLETE, PageOutcome
from .output import write
from .proxypool import ProxyPool, read_proxies_file
from .since import diff_rows
from .verify import (EXIT_EMPTY, EXIT_ERROR, EXIT_INCOMPLETE, EXIT_OK,
                     print_report, read_records, verify_file, verify_records)

EXIT_BLOCKED = 3

# run's exit contract, keyed by ScrapeResult.status().
_STATUS_EXITS = {
    "complete": EXIT_OK,
    "incomplete": EXIT_INCOMPLETE,
    "too_few_rows": EXIT_EMPTY,
    "blocked": EXIT_EMPTY,
    "error": EXIT_ERROR,
}


def exit_code(status: str, accept_partial: bool) -> int:
    """Map a run status to an exit code (incomplete -> 0 with --accept-partial)."""
    code = _STATUS_EXITS.get(status, EXIT_ERROR)
    if status == "incomplete" and accept_partial:
        return EXIT_OK
    return code


def write_report(path: str, outcomes: list[PageOutcome]) -> None:
    """One JSON object per page (JSONL), written whatever the run's fate."""
    with open(path, "w", encoding="utf-8") as f:
        for outcome in outcomes:
            f.write(json.dumps(outcome.to_dict(), ensure_ascii=False) + "\n")


def cmd_probe(args):
    res = probe(args.url)
    print(f"url:      {res.url}")
    print(f"status:   {res.status}")
    print(f"body:     {res.body_size} bytes")
    print(f"tier:     {res.tier}")
    print(f"confident:{res.confident}")
    print(f"markers:  {res.markers}")
    if res.note:
        print(f"note:     {res.note}")
    for e in res.embedded:
        detail = e.get("types") or ", ".join(
            f"{path or '(top level)'} ({n})" for path, n in e.get("lists", []))
        print(f"json:     {e['source']}: {detail or 'present'}")
    if res.embedded:
        print("          -> items: {json: <source>, path: ...} reads these "
              "without a browser (see docs/selectors.md)")
    # --fail-blocked makes the probe path usable in scripts: a walled target
    # is a loud non-zero exit, not a silent success.
    if args.fail_blocked and (res.tier >= 3 or not res.confident):
        print(f"[probe] FAIL: tier {res.tier}, not confidently reachable",
              file=sys.stderr)
        sys.exit(EXIT_BLOCKED)


def _run_config(args):
    """(config, use_env, proxies_from_file, proxy_source) for `run`.

    A config path is returned untouched when no proxy flag is given, so every
    other run keeps loading the file exactly once.

    Proxy precedence (R35): ``--no-proxy`` > ``--proxies FILE`` >
    ``CERTAINLYSCRAPED_PROXIES`` > the config. ``--no-proxy`` strips the
    config's proxies *and* makes scrape() ignore the environment; the
    explicit file is the whole set and outranks the environment too.
    """
    if not getattr(args, "proxies", None) \
            and not getattr(args, "no_proxy", False):
        return args.config, True, False, "auto"
    cfg = load_config(args.config)
    if getattr(args, "proxies", None):
        # The file is the whole proxy set: it replaces the config's, and a
        # password in it is allowed (a proxies file is not the committed one).
        cfg = {key: value for key, value in cfg.items() if key != "proxy"}
        cfg["proxies"] = read_proxies_file(args.proxies)
        return cfg, False, True, "flag"
    cfg = {key: value for key, value in cfg.items()
           if key not in ("proxy", "proxies")}
    return cfg, False, False, "auto"


def _redact_exc(exc: BaseException, cfg, *, allow_userinfo: bool = False,
                base_dir: str = ".") -> str:
    """`Type: message` with any proxy credential or address removed.

    ``base_dir`` is the config file's directory: a relative
    ``proxies: {list: proxies.txt}`` only resolves there, and a pool that
    could not be built masks nothing but the userinfo.
    """
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(cfg, str):
        base_dir = str(Path(cfg).resolve().parent)
        try:
            cfg = load_config(cfg)
        except Exception:  # noqa: BLE001 - the original error is the news
            cfg = None
    try:
        pool = (ProxyPool.from_config(cfg, base_dir=base_dir,
                                      allow_userinfo=allow_userinfo)
                if isinstance(cfg, dict) else None)
    except Exception:  # noqa: BLE001 - a bad proxies config must not hide it
        pool = None
    return pool.scrub(detail) if pool is not None else redact(detail)


def cmd_run(args):
    cfg = None
    from_file = False
    # A relative ``proxies: {list: ...}`` in the config resolves against the
    # config's own directory -- and against the proxies file's when --proxies
    # supplied the whole set.
    base_dir = str(Path(args.config).resolve().parent)
    if getattr(args, "proxies", None):
        base_dir = str(Path(args.proxies).resolve().parent)
    try:
        cfg, use_env, from_file, proxy_source = _run_config(args)
        kwargs = {}
        if isinstance(cfg, dict):
            kwargs["base_dir"] = base_dir
        if not use_env:
            kwargs["use_env"] = False
        if from_file:
            kwargs["proxies_from_file"] = True
        if proxy_source != "auto":
            kwargs["proxy_source"] = proxy_source
        result = scrape(cfg, log=sys.stderr, **kwargs)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as e:
        # A YAML parse error quotes the offending line, which may hold a
        # proxy URL: same redaction as a fetch failure.
        detail = _redact_exc(e, cfg, allow_userinfo=from_file,
                             base_dir=base_dir)
        print(f"[run] config error: {detail}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except Exception as e:  # noqa: BLE001 - network/TLS errors from any tier
        detail = _redact_exc(e, cfg, allow_userinfo=from_file,
                             base_dir=base_dir)
        print(f"[run] fetch error: {detail}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    cfg, rows = result.config, result.rows

    # Verify by data, not by exit code: assert the row count on everything
    # scraped (before --since filtering, so a quiet day with no new rows is
    # not a failure but a broken selector still is).
    res = verify_records(rows, min_rows=args.min_rows, sample=args.sample,
                         source=args.out)

    out_rows = rows
    if args.since:
        try:
            previous = read_records(args.since)
        except FileNotFoundError:
            previous = []
            print(f"[since] {args.since} not found: every row is new",
                  file=sys.stderr)
        out_rows, counts = diff_rows(rows, previous, args.key)
        print(f"[since] {counts['new']} new, {counts['changed']} changed, "
              f"{counts['unchanged']} unchanged vs {args.since}",
              file=sys.stderr)

    write(out_rows, args.out, cfg.get("output", "csv"))
    if args.snapshot:
        write(rows, args.snapshot, _format_for(args.snapshot, cfg))
        print(f"[snapshot] {args.snapshot} ({len(rows)} rows)", file=sys.stderr)
    saved = f"[saved] {args.out} ({len(out_rows)} rows)"
    print(saved, file=sys.stderr if args.out == "-" else sys.stdout)

    print_report(res, stream=sys.stderr)

    # The report is evidence about the run, so it lands before any exit
    # decision: a 2 or a 4 still leaves the per-page record on disk.
    if args.report:
        try:
            write_report(args.report, result.page_report)
        except OSError as e:
            print(f"[run] report error: {type(e).__name__}: {e}",
                  file=sys.stderr)
            sys.exit(EXIT_ERROR)

    # Verified data is the primary contract: too few rows is always 2, even
    # when pages were also incomplete.
    if not res.ok:
        sys.exit(EXIT_EMPTY)

    status = result.status(min_rows=args.min_rows)
    code = exit_code(status, args.accept_partial)
    if code == EXIT_INCOMPLETE:
        n = sum(1 for o in result.page_report if o.outcome in INCOMPLETE)
        print(f"[run] incomplete: {n} page(s) blocked/rate-limited/errored; "
              f"rows may be missing (exit 4; --accept-partial to accept)",
              file=sys.stderr)
    if code:
        sys.exit(code)


def cmd_mcp(args):
    """Run the MCP server over stdio: the agent-facing surface."""
    if importlib.util.find_spec("mcp") is None:
        print('pip install "certainlyscraped[mcp]"', file=sys.stderr)
        sys.exit(EXIT_ERROR)
    from .mcp_server import build_server

    pool = _server_pool(args)
    workdir = Path(args.workdir) if args.workdir else Path.cwd()
    build_server(workdir, allow_private=args.allow_private,
                 pool=pool).run("stdio")


def _server_pool(args):
    """The server's own proxy pool from `mcp --proxies PATH` (never an agent's).

    The file is a dedicated proxies file, so its URLs may carry a password
    (R31). Precedence is `run`'s (R35): the explicit ``--proxies`` file is the
    whole set and outranks ``CERTAINLYSCRAPED_PROXIES``, which would otherwise
    silently replace it (``proxy_source="flag"``). ``mcp`` has no
    ``--no-proxy``: without ``--proxies`` the server has no pool at all, so
    there is nothing to switch off. The environment is not honoured for
    ``mcp`` -- it is not an input to the server's pool.
    """
    path = getattr(args, "proxies", None)
    if not path:
        return None
    try:
        pool = ProxyPool.from_config(
            {"proxies": read_proxies_file(path)},
            base_dir=str(Path(path).resolve().parent), allow_userinfo=True,
            proxy_source="flag")
    except (OSError, ValueError) as e:
        print(f"[mcp] proxies error: {redact(str(e))}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    if pool is None:
        print(f"[mcp] {path}: no proxies found", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    return pool


def _format_for(path: str, cfg: dict) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext if ext in ("csv", "json", "jsonl") else cfg.get("output", "csv")


def cmd_verify(args):
    res = verify_file(args.path, fmt=args.format,
                      min_rows=args.min_rows, sample=args.sample)
    print_report(res)
    if not res.ok:
        sys.exit(EXIT_EMPTY)


def main(argv=None):
    p = argparse.ArgumentParser(prog="certainlyscraped")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="classify a site's anti-bot tier")
    p_probe.add_argument("url")
    p_probe.add_argument("--fail-blocked", action="store_true",
                         help="exit non-zero (3) when the target is walled")
    p_probe.set_defaults(func=cmd_probe)

    p_batch = sub.add_parser("batchprobe", help="classify many sites -> CSV")
    batchprobe.add_arguments(p_batch)
    p_batch.set_defaults(func=batchprobe.run)

    p_page = sub.add_parser("pageprobe", help="certify pagination advances -> CSV")
    pageprobe.add_arguments(p_page)
    p_page.set_defaults(func=pageprobe.run)

    p_init = sub.add_parser(
        "init", help="guess a config from a listing page and preview it")
    assist.add_init_arguments(p_init)
    p_init.set_defaults(func=assist.run_init)

    p_try = sub.add_parser(
        "try", help="test a CSS selector against a page or saved file")
    assist.add_try_arguments(p_try)
    p_try.set_defaults(func=assist.run_try)

    p_fetch = sub.add_parser(
        "fetch", help="save a page's HTML (what certainlyscraped sees) to a file")
    assist.add_fetch_arguments(p_fetch)
    p_fetch.set_defaults(func=assist.run_fetch)

    p_run = sub.add_parser(
        "run", help="run a YAML scrape config",
        epilog="exit codes: 0 ok, 1 error, 2 too few rows, 4 rows verified "
               "but some pages were blocked, rate-limited or errored "
               "(--accept-partial to accept those runs)")
    p_run.add_argument("config")
    p_run.add_argument("--out", default="out.csv",
                       help="dataset path, or - for stdout (default out.csv)")
    p_run.add_argument("--min-rows", type=int, default=1,
                       help="fail if fewer rows are extracted (default 1)")
    p_run.add_argument("--sample", type=int, default=3,
                       help="records to print as a sample (default 3)")
    p_run.add_argument("--since", metavar="PREVIOUS",
                       help="only output rows new or changed vs this earlier "
                            "dataset (adds a _change column)")
    p_run.add_argument("--key", action="append", metavar="FIELD",
                       help="field(s) identifying a row for --since "
                            "(repeatable; default: the whole row)")
    p_run.add_argument("--snapshot", metavar="PATH",
                       help="also write every scraped row here; pass the "
                            "same path as --since to keep a rolling state file")
    p_run.add_argument("--report", metavar="PATH",
                       help="write one JSON line per page (outcome, status, "
                            "rows, attempts, steps, proxy_group, elapsed_ms)")
    p_run.add_argument("--accept-partial", action="store_true",
                       help="exit 0 when some pages were blocked, "
                            "rate-limited or errored (default: exit 4)")
    p_proxies = p_run.add_mutually_exclusive_group()
    p_proxies.add_argument("--proxies", metavar="PATH",
                           help="proxy list file (YAML, or one URL per line "
                                "with optional '# group' labels); replaces "
                                "the config's proxy/proxies")
    p_proxies.add_argument("--no-proxy", action="store_true",
                           help="ignore every proxy in the config and "
                                "fetch directly")
    p_run.set_defaults(func=cmd_run)

    p_verify = sub.add_parser(
        "verify", help="assert a dataset file is non-empty and show a sample")
    p_verify.add_argument("path", help="CSV / JSON / JSONL dataset file")
    p_verify.add_argument("--format", choices=["csv", "json", "jsonl"],
                          default=None, help="default: infer from extension")
    p_verify.add_argument("--min-rows", type=int, default=1)
    p_verify.add_argument("--sample", type=int, default=3)
    p_verify.set_defaults(func=cmd_verify)

    p_mcp = sub.add_parser(
        "mcp", help="run the MCP server over stdio (for AI agents)",
        description="Serve probe/init/try_selector/scrape over MCP (stdio). "
                    "Private addresses are refused and configs are inline "
                    "YAML/JSON text only.")
    p_mcp.add_argument("--allow-private", action="store_true",
                       help="allow loopback and private-range targets "
                            "(local development; never the default)")
    p_mcp.add_argument("--workdir", metavar="DIR", default=None,
                       help="directory the dataset files go under "
                            "(default: the current directory)")
    p_mcp.add_argument("--proxies", metavar="PATH",
                       help="the server's own proxy list file (YAML, or one "
                            "URL per line with optional '# group' labels); "
                            "agents can never supply proxies")
    p_mcp.set_defaults(func=cmd_mcp)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
