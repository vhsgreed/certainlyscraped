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
  a server-side decision (a later `--proxies` flag), never a tool argument.

stdout is the JSON-RPC channel: no `print` in this module, and every call into
the library passes `log=None`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

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
})
REFUSED_CONFIG_KEYS = frozenset({
    "proxy", "proxies", "cookies", "since", "snapshot", "fetcher",
})

MAX_TIER = 2
MAX_ROWS_CAP = 500
DEFAULT_MAX_ROWS = 50
MAX_PAGES_CAP = 1000
MAX_TIMEOUT = 120


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
    if not isinstance(url, str) or not url.strip():
        raise _ConfigError("url must be a non-empty string")
    url = url.strip()
    if url.lower().startswith("file://"):
        raise _ConfigError("url: file:// is refused; only http:// and "
                           "https:// URLs are accepted")
    if not url.lower().startswith(("http://", "https://")):
        raise _ConfigError("url must be an http:// or https:// URL "
                           "(local paths and file:// are not readable)")
    return url


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
    cfg["url"] = _check_target(cfg.get("url"))
    for key in ("items", "fields"):
        if key in cfg and not isinstance(cfg[key], dict):
            raise _ConfigError(f"{key} must be a mapping")
    for key in ("max_pages", "timeout"):
        if key in cfg:
            try:
                cfg[key] = int(cfg[key])
            except (TypeError, ValueError):
                raise _ConfigError(f"{key} must be an integer") from None
    # Bounds, not policy: one tool call may not occupy the server forever.
    if cfg.get("max_pages", 0) > MAX_PAGES_CAP:
        cfg["max_pages"] = MAX_PAGES_CAP
        notes.append(f"max_pages capped at {MAX_PAGES_CAP}")
    if cfg.get("timeout", 0) > MAX_TIMEOUT:
        cfg["timeout"] = MAX_TIMEOUT
        notes.append(f"timeout capped at {MAX_TIMEOUT}s")

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
    except (TypeError, ValueError):
        raise _ConfigError(f"{name} must be an integer") from None
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


def build_server(workdir: Path, allow_private: bool = False, *,
                 _scrape=None, _probe=None, _load_page=None) -> MCPServer:
    """A stdio-ready MCP server whose reads and writes stay in `workdir`.

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

    server = MCPServer(name="certainlyscraped", version=__version__)

    @server.tool()
    def probe(url: str) -> dict:
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
    def init(url: str) -> dict:
        """Draft a scrape config for a listing page and return it as YAML text.

        Returns {status: ok|error, config (YAML text, ready to pass to
        `scrape`), items (guessed items selector), fields (guessed field
        specs), rows_preview (first 3 extracted rows), note (item count and
        per-field coverage)}. The selectors are guesses: check the coverage
        and test a field with try_selector before trusting them.
        """
        try:
            target = _check_target(url)
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
    def try_selector(url: str, selector: str,
                     within: str | None = None) -> dict:
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
    def scrape(config: str, min_rows: int = 1,
               max_rows: int = DEFAULT_MAX_ROWS) -> dict:
        """Run an inline YAML/JSON scrape config and return rows plus a dataset.

        `config` is config TEXT (not a path). Allowed keys: url, items,
        fields, json, paginate, max_pages, rate_limit_sec, timeout, tier,
        output, headers. `proxy`/`cookies`/`since`/`snapshot`/`fetcher` are
        refused and tier is capped at 2. Returns {status: complete |
        incomplete | too_few_rows | blocked | error, rows (up to max_rows,
        default 50, cap 500), row_count (the true total), dataset_path (full
        JSONL in the server's working directory), pages (per-page outcomes),
        notes}. Park the full dataset path; the response is truncated.
        """
        extra = {"rows": [], "row_count": 0, "dataset_path": None, "pages": []}
        try:
            cfg, notes = _check_config(config)
            min_rows, _ = _int_arg("min_rows", min_rows, 1, 1)
            max_rows, more = _int_arg("max_rows", max_rows,
                                      DEFAULT_MAX_ROWS, 1, MAX_ROWS_CAP)
            notes += more

            result = scrape_impl(cfg, guard=guard, log=None)
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
