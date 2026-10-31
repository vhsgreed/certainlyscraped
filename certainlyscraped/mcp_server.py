"""The MCP server: what an AI agent is allowed to ask this library to do.

Four tools -- `probe`, `init`, `try_selector`, `scrape` -- each a thin call
into the existing API. The agent-facing contract differs from the CLI in three
ways, all deliberate:

* **No exception escapes a tool.** Every result is a dict with a `status`
  (`complete|incomplete|too_few_rows|blocked|error` where a scrape ran,
  `ok|blocked|error` for a probe, `ok|nothing|error` for a selector) and a
  `notes` list. A traceback would end a JSON-RPC request, not a shell script.
* **Nothing is read from or written to arbitrary paths.** Configs arrive as
  YAML/JSON *text*, never a path; `file://` is refused; the only write is the
  full JSONL dataset under `workdir/.certainlyscraped/`.
* **The guard is always on** (`netguard.check_url`, every hop) and the tier is
  capped at 2. A browser cannot be policed per hop, and a proxy/cookie jar is
  a server-side decision (`--proxies`, which builds the pool this module is
  handed), never a tool argument.

stdout is the JSON-RPC channel: no `print` in this module, and every call into
the library passes `log=None`.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from mcp.server.mcpserver import MCPServer

from . import __version__
from .api import scrape as api_scrape
from .assist import (_name_for, coverage_report,
                     load_page as assist_load_page, render_config)
from .classify import probe as classify_probe
from .extract import extract_page
from .netguard import PrivateAddressError, check_url
from .suggest import explain_matches, suggest

# What an agent may put in a config. Everything else is either a capability
# the operator keeps (proxy, cookies, fetcher) or a path (since, snapshot).
ALLOWED_CONFIG_KEYS = frozenset({
    "url", "items", "fields", "json", "paginate", "max_pages",
    "rate_limit_sec", "timeout", "tier", "output", "headers",
    # Page sources (R17): the parallel runner's own shapes.
    "pages", "urls", "concurrency",
    # Escalation ladder and proxy pool knobs (R21).
    "max_tier", "max_retries", "cooldown_sec", "per_proxy_rate_sec",
})
REFUSED_CONFIG_KEYS = frozenset({
    "proxy", "proxies", "cookies", "since", "snapshot", "fetcher",
})

MAX_TIER = 2
MAX_ROWS_CAP = 500
DEFAULT_MAX_ROWS = 50
# follow_links itself clamps with min(cfg max_pages, 100): 100 is the real
# ceiling, so announcing anything higher would be a lie. runner.RANGE_CAP is
# the same 100 for a `pages:` template range.
MAX_PAGES_CAP = 100
MAX_RANGE_CAP = 100
MAX_URLS = 100
DEFAULT_MAX_PAGES = 10
DEFAULT_RATE_LIMIT = 2.0
MAX_RATE_LIMIT = 30.0
DEFAULT_TIMEOUT = 30.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 120.0
MAX_CONCURRENCY = 16
DEFAULT_MAX_RETRIES = 3
MAX_MAX_RETRIES = 3
# The ladder's own default is 300 s. That is fine on the CLI, where Ctrl-C
# reaches the sleep; inline on the sync MCP server it would block the whole
# tool call, so the server pins the default and the cap at 60.
DEFAULT_COOLDOWN = 60.0
MAX_COOLDOWN = 60.0
MAX_PER_PROXY_RATE = 30.0


class _ConfigError(ValueError):
    """A request the server refuses to run, with an agent-readable message."""


def _error(note: str, **extra) -> dict:
    out = {"status": "error", "notes": [note]}
    out.update(extra)
    return out


def _probe_error(msg: str, *, private_refused: bool = False) -> dict:
    """Probe failures carry `note` (singular), like a successful probe does."""
    return {"status": "error", "notes": [msg], "note": msg, "tier": None,
            "body_size": 0, "markers": [], "embedded": [],
            "private_refused": private_refused}


def _jsonable(value):
    """Plain JSON types only: tool arguments can carry odd scalars."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _check_target(url) -> str:
    """An http(s) URL string, or _ConfigError. Never a path, never file://."""
    if not isinstance(url, str):
        raise _ConfigError("url must be a string")
    if not url.strip():
        raise _ConfigError("url must be a non-empty string")
    url = url.strip()
    if url.lower().startswith("file://"):
        raise _ConfigError("url: file:// is refused; only http:// and "
                           "https:// URLs are accepted")
    if not url.lower().startswith(("http://", "https://")):
        raise _ConfigError("url must be an http:// or https:// URL "
                           "(local paths and file:// are not readable)")
    return url


def _bounded_float(name: str, value, default: float, low: float, high: float,
                   notes: list[str], *, low_is_default: bool = False) -> float:
    """A finite float in [low, high], or `default` with a note.

    Never raises: this runs before the first fetch, so a note is enough, and
    the library only ever sees a value it cannot choke on. `low_is_default`
    makes a below-range value fall back to the default instead of clamping up
    (a negative delay means "you typed nonsense", not "go as fast as you can").
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        notes.append(f"{name} is not a number ({value!r}); "
                     f"using the default {default}")
        return default
    if not math.isfinite(n):
        notes.append(f"{name} must be a finite number (got {value!r}); "
                     f"using the default {default}")
        return default
    if n < low:
        if low_is_default:
            notes.append(f"{name} must be a number >= {low:g} "
                         f"(got {value!r}); using the default {default}")
            return default
        notes.append(f"{name} clamped to {low:g}")
        return float(low)
    if n > high:
        notes.append(f"{name} clamped to {high:g}")
        return float(high)
    return n


def _bounded_pages(value, notes: list[str]) -> int:
    """max_pages as an int in [1, MAX_PAGES_CAP]; follow_links caps at 100."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        notes.append(f"max_pages is not a whole number ({value!r}); "
                     f"using the default {DEFAULT_MAX_PAGES}")
        return DEFAULT_MAX_PAGES
    if n < 1:
        notes.append("max_pages must be at least 1; using 1")
        return 1
    if n > MAX_PAGES_CAP:
        notes.append(f"max_pages capped at {MAX_PAGES_CAP} "
                     "(the pagination loop's own ceiling)")
        return MAX_PAGES_CAP
    return n


def _bounded_int(name: str, value, default: int, low: int, high: int,
                 notes: list[str]) -> int:
    """An int in [low, high], or `default` with a note. Never raises.

    Same contract as :func:`_bounded_float`: this runs before the first
    fetch, so a note is enough and the library cannot choke on the value.
    A fractional value is truncated, and the note says so: `3.7` silently
    becoming 3 looked like the request had been honoured.
    """
    truncated = (value if isinstance(value, float)
                 and math.isfinite(value) and not value.is_integer()
                 else None)
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        notes.append(f"{name} is not a whole number ({value!r}); "
                     f"using the default {default}")
        return default
    if truncated is not None:
        notes.append(f"{name} {truncated!r} truncated to {n}")
    if n < low:
        notes.append(f"{name} clamped to {low}")
        return low
    if n > high:
        notes.append(f"{name} clamped to {high}")
        return high
    return n


def _host_problem(host) -> str | None:
    """Why `host` cannot be fetched as a literal host, or None if it can.

    An empty host dies in the guard after the config was accepted, and a host
    made of a `{...}` placeholder (``http://{n}``) is never a host at all: the
    URL is used as written, so the fetch would go to a DNS name with braces.
    """
    if not host:
        return "must name a host, not just a scheme"
    if "{" in host or "}" in host:
        return "must name a real host, not a {placeholder}"
    return None


def _check_pages(value, notes: list[str]) -> dict:
    """A `pages:` mapping, validated and range-capped before any fetch.

    The runner would raise on most of this only after the scrape started
    (page_source runs inside api.scrape), and its range cap is announced on
    a log stream the MCP server does not have; so the shape is checked here.
    """
    if not isinstance(value, dict):
        raise _ConfigError("pages must be a mapping with template, from and to")
    template = value.get("template")
    if not isinstance(template, str) or "{n}" not in template:
        raise _ConfigError("pages.template must be a string containing {n}")
    if not template.lower().startswith(("http://", "https://")):
        raise _ConfigError("pages.template must be an absolute http:// or "
                           "https:// URL containing {n}")
    problem = _host_problem(urlsplit(template).hostname)
    if problem:
        raise _ConfigError(f"pages.template {problem}: {template!r}")
    bounds = {}
    for key in ("from", "to"):
        raw = value.get(key, 1)
        try:
            bounds[key] = int(raw)
        except (TypeError, ValueError, OverflowError):
            raise _ConfigError(
                f"pages.{key} must be a whole number (got {raw!r})") from None
    start, stop = bounds["from"], bounds["to"]
    if stop < start:
        raise _ConfigError(
            f"pages.from ({start}) is greater than pages.to ({stop})")
    if stop - start + 1 > MAX_RANGE_CAP:
        stop = start + MAX_RANGE_CAP - 1
        notes.append(f"pages range capped at {MAX_RANGE_CAP} pages "
                     f"(from {start} to {stop})")
    return {"template": template.strip(), "from": start, "to": stop}


def _check_urls(value, notes: list[str]) -> list[str]:
    """A `urls:` list of absolute http(s) strings, capped at MAX_URLS.

    An entry that is not an absolute http(s) URL is refused by index, before
    the first fetch: the runner would stringify it and the guard would then
    fail one page after the others were already paid for.
    """
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise _ConfigError("urls must be a list of absolute http(s) URLs")
    if not value:
        raise _ConfigError("urls is empty: list at least one URL")
    out: list[str] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise _ConfigError(
                f"urls[{i}] must be a non-empty string (got {entry!r})")
        url = entry.strip()
        if not url.lower().startswith(("http://", "https://")):
            raise _ConfigError(
                f"urls[{i}] is not an absolute http:// or https:// URL: "
                f"{url!r}")
        problem = _host_problem(urlsplit(url).hostname)
        if problem:
            raise _ConfigError(f"urls[{i}] {problem}: {url!r}")
        out.append(url)
    if len(out) > MAX_URLS:
        notes.append(f"urls capped at {MAX_URLS} of {len(out)}")
        out = out[:MAX_URLS]
    return out


def _check_config(text) -> tuple[dict, list[str]]:
    """Parse an inline config, enforce the allow-list, return (cfg, notes)."""
    if isinstance(text, dict):  # defensive: a client that bypassed the schema
        raw = text
    else:
        if not isinstance(text, str) or not text.strip():
            raise _ConfigError("config is required: inline YAML/JSON text "
                               "(paths are not accepted)")
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise _ConfigError(f"config is not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise _ConfigError("config must be a YAML/JSON mapping")

    notes: list[str] = []
    allowed = ", ".join(sorted(ALLOWED_CONFIG_KEYS))
    for key in raw:
        if key in REFUSED_CONFIG_KEYS:
            raise _ConfigError(
                f"{key}: refused in an agent's config. Proxies, cookies, "
                "previous datasets and fetchers are server-side settings; "
                f"allowed keys are: {allowed}")
        if key not in ALLOWED_CONFIG_KEYS:
            raise _ConfigError(f"unknown config key {key!r}; allowed keys "
                               f"are: {allowed}")

    cfg = dict(raw)
    # Exactly one page source may be named (runner.page_source enforces this
    # too, but it runs inside api.scrape, after the server has said yes).
    sources = [key for key in ("paginate", "pages", "urls")
               if cfg.get(key) is not None]
    if len(sources) > 1:
        raise _ConfigError(
            "config sets more than one page source ("
            + ", ".join(sources)
            + "): use exactly one of paginate, pages, urls")
    if "pages" in cfg:
        cfg["pages"] = _check_pages(cfg["pages"], notes)
    if "urls" in cfg:
        cfg["urls"] = _check_urls(cfg["urls"], notes)
    # A pages:/urls: config has no base url of its own; the API uses the
    # first expanded page as the base. Only check a url that was given.
    if cfg.get("url") is not None:
        cfg["url"] = _check_target(cfg["url"])
    elif "pages" not in cfg and "urls" not in cfg:
        _check_target(cfg.get("url"))  # raises with the usual message
    for key in ("items", "fields", "paginate"):
        if key in cfg and not isinstance(cfg[key], dict):
            # paginate is read *after* the first page is fetched; a bad type
            # there would throw away pages already paid for. items/fields are
            # read on the first page, so the same argument applies.
            raise _ConfigError(f"{key} must be a mapping")
    # Everything below is normalised before the first request: a bad value
    # that reached the library would either sleep for hours, or raise inside
    # the pagination loop after the rows were already fetched.
    if "max_pages" in cfg:
        cfg["max_pages"] = _bounded_pages(cfg["max_pages"], notes)
    if "rate_limit_sec" in cfg:
        cfg["rate_limit_sec"] = _bounded_float(
            "rate_limit_sec", cfg["rate_limit_sec"], DEFAULT_RATE_LIMIT,
            0.0, MAX_RATE_LIMIT, notes, low_is_default=True)
    if "timeout" in cfg:
        cfg["timeout"] = _bounded_float(
            "timeout", cfg["timeout"], DEFAULT_TIMEOUT, MIN_TIMEOUT,
            MAX_TIMEOUT, notes)
    if "concurrency" in cfg:
        cfg["concurrency"] = _bounded_int(
            "concurrency", cfg["concurrency"], 1, 1, MAX_CONCURRENCY, notes)
    if "max_tier" in cfg:
        try:
            asked_over = int(cfg["max_tier"]) > MAX_TIER
        except (TypeError, ValueError, OverflowError):
            asked_over = False
        cfg["max_tier"] = _bounded_int(
            "max_tier", cfg["max_tier"], MAX_TIER, 1, MAX_TIER, notes)
        if asked_over:
            notes.append(
                f"tier 3 is not reachable through the MCP guard; "
                f"max_tier stays at {MAX_TIER}")
    if "max_retries" in cfg:
        cfg["max_retries"] = _bounded_int(
            "max_retries", cfg["max_retries"], DEFAULT_MAX_RETRIES, 0,
            MAX_MAX_RETRIES, notes)
    # Always pinned: the ladder's own default is 300 s, which is fine on the
    # CLI and unacceptable inline on the server. Applied silently when the
    # config omits the key (the cap is the server's, not the user's value).
    cfg["cooldown_sec"] = _bounded_float(
        "cooldown_sec", cfg.get("cooldown_sec", DEFAULT_COOLDOWN),
        DEFAULT_COOLDOWN, 0.0, MAX_COOLDOWN, notes)
    if "per_proxy_rate_sec" in cfg:
        cfg["per_proxy_rate_sec"] = _bounded_float(
            "per_proxy_rate_sec", cfg["per_proxy_rate_sec"], 1.0, 0.0,
            MAX_PER_PROXY_RATE, notes)

    tier = cfg.get("tier", "auto")
    if tier != "auto":
        try:
            tier = int(tier)
        except (TypeError, ValueError):
            raise _ConfigError("tier must be auto, 1 or 2") from None
        if tier < 1:
            raise _ConfigError("tier must be auto, 1 or 2")
        if tier > MAX_TIER:
            tier = MAX_TIER
            notes.append(f"max_tier clamped to {MAX_TIER}: the private-address "
                         "guard cannot police every hop of a browser (tier 3)")
        cfg["tier"] = tier
    return cfg, notes


def _int_arg(name: str, value, default: int, low: int, high: int | None = None):
    if value is None:
        return default, []
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        # A mistyped tool argument must not cost the agent its call: fall back
        # to the documented default and say so.
        return default, [f"{name} must be a whole number (got {value!r}); "
                         f"using the default {default}"]
    notes = []
    if n < low:
        n = low
    if high is not None and n > high:
        n = high
        notes.append(f"{name} capped at {high}")
    return n, notes


def _dataset_path(out_dir: Path, cfg: dict) -> Path:
    name = _name_for(str(cfg.get("url", "site"))) or "site"
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "site"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{name}-{stamp}.jsonl"
    n = 1
    while path.exists():  # two calls in the same second
        path = out_dir / f"{name}-{stamp}-{n}.jsonl"
        n += 1
    return path


def _write_dataset(workdir: Path, out_dir: Path, cfg: dict,
                   rows: list[dict]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    root = workdir.resolve()
    target = out_dir.resolve()
    if target != root and root not in target.parents:
        # `.certainlyscraped` was pre-created as a symlink pointing elsewhere.
        raise _ConfigError(
            "refusing to write the dataset outside the working directory: "
            f"{out_dir} resolves to {target}")
    path = _dataset_path(out_dir, cfg)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def _drop_name(text: str) -> str:
    """render_config emits a `name:` label, which `scrape` does not accept."""
    return "\n".join(line for line in text.splitlines()
                     if not line.startswith("name: "))


def build_server(workdir: Path, allow_private: bool = False, *, pool=None,
                 _scrape=None, _probe=None, _load_page=None) -> MCPServer:
    """A stdio-ready MCP server whose reads and writes stay in `workdir`.

    ``pool`` is the server operator's own ProxyPool (`mcp --proxies`): every
    scrape goes through it, and an agent's config can never name a proxy.

    _scrape/_probe/_load_page are test seams with the signatures of
    api.scrape / classify.probe / assist.load_page; production leaves them None.
    """
    workdir = Path(workdir)
    out_dir = workdir / ".certainlyscraped"
    scrape_impl = _scrape or api_scrape
    probe_impl = _probe or classify_probe
    load_impl = _load_page or assist_load_page

    def guard(url: str) -> None:
        check_url(url, allow_private=allow_private)

    def lease(url: str):
        """(proxy, host) from the server's own pool, or (None, host).

        R40: when the operator gave the server a pool, every request —
        including the probe and the page load behind init/try_selector —
        leaves through a leased proxy. No lease means no request at all.
        """
        host = urlsplit(url).hostname or ""
        return pool.acquire(host), host

    server = MCPServer(name="certainlyscraped", version=__version__)

    @server.tool()
    def probe(url: Any = None) -> dict:
        """Classify a page's anti-bot defences before scraping it.

        Returns {status: ok|blocked|error, tier (1-2 reachable here; 3 means
        walled), note, body_size, markers, private_refused, embedded}. Call
        this first: it decides whether scraping is worth it and whether
        embedded JSON could replace selectors. Private/loopback addresses are
        refused (status error, private_refused true) unless the server was
        started with --allow-private.
        """
        try:
            target = _check_target(url)
            if pool is not None:
                proxy, host = lease(target)
                if proxy is None:
                    note = f"no proxy available for {host}"
                    return {"status": "blocked", "tier": None, "note": note,
                            "body_size": 0, "markers": [],
                            "private_refused": False, "embedded": []}
                res = probe_impl(target, guard=guard, proxy=proxy)
            else:
                res = probe_impl(target, guard=guard)
        except PrivateAddressError as e:
            return _probe_error(f"refused: {e}", private_refused=True)
        except _ConfigError as e:
            return _probe_error(str(e))
        except Exception as e:  # noqa: BLE001 - a tool never raises
            return _probe_error(f"{type(e).__name__}: {e}")
        tier = getattr(res, "tier", None)
        confident = bool(getattr(res, "confident", False))
        blocked = tier is None or tier >= 3 or not confident
        return {
            "status": "blocked" if blocked else "ok",
            "tier": tier,
            "note": getattr(res, "note", "") or "",
            "body_size": getattr(res, "body_size", 0) or 0,
            "markers": list(getattr(res, "markers", []) or []),
            "private_refused": False,
            "embedded": [e.get("source") for e in
                         (getattr(res, "embedded", None) or [])],
        }

    @server.tool()
    def init(url: Any = None) -> dict:
        """Draft a scrape config for a listing page and return it as YAML text.

        Returns {status: ok|error, config (YAML text, ready to pass to
        `scrape`), items (guessed items selector), fields (guessed field
        specs), rows_preview (first 3 extracted rows), note (item count and
        per-field coverage)}. The selectors are guesses: check the coverage
        and test a field with try_selector before trusting them.
        """
        try:
            target = _check_target(url)
            if pool is not None:
                proxy, host = lease(target)
                if proxy is None:
                    return {"status": "blocked", "config": "", "items": {},
                            "fields": {}, "rows_preview": [],
                            "note": f"no proxy available for {host}"}
                target, html, tier = load_impl(target, tier="auto", log=None,
                                               guard=guard, proxy=proxy)
            else:
                target, html, tier = load_impl(target, tier="auto", log=None,
                                               guard=guard)
            s = suggest(html)
            if s is None:
                return {"status": "error", "config": "", "items": {},
                        "fields": {}, "rows_preview": [],
                        "note": "no repeated item blocks found on this page; "
                                "use try_selector to test a selector you "
                                "picked from the page"}
            for field_name in ("url", "image"):
                spec = s.fields.get(field_name)
                if spec is not None and "path" not in spec:
                    spec["absolute"] = True
            cfg = {"items": s.items, "fields": s.fields}
            rows = extract_page(html, cfg, target)
            tier = tier if tier in (1, 2) else "auto"
            text = _drop_name(render_config(_name_for(target), target, tier,
                                            s, rows))
            note = (f"{len(rows)} items on the first page. "
                    + coverage_report(rows, s.fields))
            return {"status": "ok", "config": text, "items": s.items,
                    "fields": s.fields,
                    "rows_preview": _jsonable(rows[:3]), "note": note}
        except PrivateAddressError as e:
            return _error(f"refused: {e}", config="", items={}, fields={},
                          rows_preview=[])
        except _ConfigError as e:
            return _error(str(e), config="", items={}, fields={},
                          rows_preview=[])
        except Exception as e:  # noqa: BLE001 - a tool never raises
            return _error(f"{type(e).__name__}: {e}", config="", items={},
                          fields={}, rows_preview=[])

    @server.tool()
    def try_selector(url: Any = None, selector: Any = None,
                     within: Any = None) -> dict:
        """Test a CSS selector against the live page and show what it finds.

        Returns {status: ok|nothing|error, matches, values (up to 20), tags,
        warnings, hints}. Pass `within` (an items selector) to test how a
        field selector resolves inside each item, exactly as scrape would.
        A malformed selector is an error result, never an exception.
        """
        empty = {"matches": 0, "values": [], "tags": [], "warnings": [],
                 "hints": []}
        try:
            target = _check_target(url)
            if not isinstance(selector, str) or not selector.strip():
                raise _ConfigError("selector must be a non-empty string")
            if within is not None and not isinstance(within, str):
                raise _ConfigError(
                    "within must be a string (an items selector), got "
                    f"{type(within).__name__}")
            if pool is not None:
                proxy, host = lease(target)
                if proxy is None:
                    note = f"no proxy available for {host}"
                    return {"status": "blocked", "note": note, **empty,
                            "hints": [note]}
                target, html, _ = load_impl(target, tier="auto", log=None,
                                            guard=guard, proxy=proxy)
            else:
                target, html, _ = load_impl(target, tier="auto", log=None,
                                            guard=guard)
            res = explain_matches(html, selector, "text",
                                  (within or None), 20)
        except PrivateAddressError as e:
            msg = f"refused: {e}"
            return _error(msg, **{**empty, "hints": [msg]})
        except _ConfigError as e:
            return _error(str(e), **{**empty, "hints": [str(e)]})
        except Exception as e:  # noqa: BLE001 - invalid selector, bad page
            msg = f"invalid selector or page: {type(e).__name__}: {e}"
            return _error(msg, **{**empty, "hints": [msg]})
        values = _jsonable(res.get("values") or [])
        return {
            "status": "ok" if res["matches"] else "nothing",
            "matches": res["matches"],
            "values": values[:20],
            "tags": [[t, n] for t, n in res.get("tags", [])],
            "warnings": res.get("warnings", []),
            "hints": res.get("hints", []),
        }

    @server.tool()
    def scrape(config: Any = None, min_rows: Any = 1,
               max_rows: Any = DEFAULT_MAX_ROWS) -> dict:
        """Run an inline YAML/JSON scrape config and return rows plus a dataset.

        `config` is config TEXT (not a path). Allowed keys: url, pages,
        urls, concurrency, items, fields, json, paginate, max_pages,
        rate_limit_sec, timeout, tier, max_tier, max_retries, cooldown_sec,
        per_proxy_rate_sec, output, headers. `proxy`/`proxies`/`cookies`/
        `since`/`snapshot`/`fetcher` are refused; tiers are capped at 2.
        A config may use pages (a {n} template range) or urls (a list)
        instead of a top-level url. Per-call bounds (notes say when one
        applied): max_pages 100, pages range 100, urls 100, concurrency
        1-16, rate_limit_sec 0-30, per_proxy_rate_sec 0-30, timeout 1-120 s,
        max_retries 0-3, cooldown_sec 0-60.
        Returns {status: complete |
        incomplete | too_few_rows | blocked | error, rows (up to max_rows,
        default 50, cap 500), row_count (the true total), dataset_path (full
        JSONL in the server's working directory), pages (per-page outcomes),
        notes}. Park the full dataset path; the response is truncated.
        """
        extra = {"rows": [], "row_count": 0, "dataset_path": None, "pages": []}
        try:
            cfg, notes = _check_config(config)
            min_rows, min_notes = _int_arg("min_rows", min_rows, 1, 1)
            max_rows, more = _int_arg("max_rows", max_rows,
                                      DEFAULT_MAX_ROWS, 1, MAX_ROWS_CAP)
            notes += min_notes + more

            # R41: the environment is not an input to an MCP server. Without
            # --proxies there is no pool at all, whatever the operator's
            # shell exports.
            call = {"guard": guard, "log": None, "use_env": False}
            if pool is not None:
                call["pool"] = pool
            result = scrape_impl(cfg, **call)
            rows = _jsonable(list(getattr(result, "rows", []) or []))
            row_count = len(rows)
            try:
                status = result.status(min_rows)
            except Exception:  # noqa: BLE001 - a fake/older result
                status = "complete" if row_count >= min_rows else "too_few_rows"
            pages = _jsonable([p.to_dict() for p in
                               (getattr(result, "page_report", None) or [])])
            path = _write_dataset(workdir, out_dir, cfg, rows)
            if row_count > max_rows:
                notes.append(f"rows truncated to {max_rows} of {row_count}; "
                             f"the full dataset is {path}")
            return {"status": status, "rows": rows[:max_rows],
                    "row_count": row_count, "dataset_path": str(path),
                    "pages": pages, "notes": notes}
        except PrivateAddressError as e:
            return _error(f"refused: {e}", private_refused=True, **extra)
        except _ConfigError as e:
            return _error(str(e), **extra)
        except Exception as e:  # noqa: BLE001 - a tool never raises
            return _error(f"{type(e).__name__}: {e}", **extra)

    return server
