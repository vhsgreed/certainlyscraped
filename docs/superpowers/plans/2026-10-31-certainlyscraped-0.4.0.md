# certainlyscraped 0.4.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn scrapeforge 0.3.0 into certainlyscraped 0.4.0: renamed, per-page outcomes with exit 4, an MCP server, parallel page sources, a per-site escalation ladder and a proxy pool.

**Architecture:** Evolve in place. A new `PageFetch`/`PageOutcome` pair becomes the unit every layer passes around: the ladder produces `PageFetch`, the page runners (next-link and parallel) turn it into `PageOutcome`, `api.scrape` aggregates outcomes into a status, and the CLI and MCP server map that status to exit codes or a `status` field. Threads, not async.

**Tech Stack:** Python >=3.11, httpx, curl_cffi, BeautifulSoup, PyYAML, pytest; `mcp>=2.3,<3` (optional extra; v2 API: `from mcp.server.mcpserver import MCPServer`, in-process test client `from mcp import Client`).

**Spec:** `docs/superpowers/specs/2026-10-31-certainlyscraped-design.md`

## Global Constraints

- Package, import and command: `certainlyscraped`; alias command `cscrape`; version `0.4.0` (`0.4.0.dev0` until Task 8).
- User-Agent: `certainlyscraped/<version> (+https://github.com/vhsgreed/certainlyscraped)`.
- `scrapeforge` command and `import scrapeforge` keep working with a `DeprecationWarning` / stderr notice; removal is 0.5.0, not this plan.
- Exit codes: 0 complete, 1 config/I-O/fetch error, 2 fewer than `--min-rows`, 3 `probe --fail-blocked`, 4 incomplete. 2 wins over 4. `--accept-partial` maps 4 to 0.
- Incomplete page outcomes are exactly `blocked`, `rate_limited`, `error` (after all retries). `not_found` and `empty` never count.
- `max_tier` default 2; `max_retries` default 3 (a page gets at most 1 + `max_retries` attempts); `cooldown_sec` default 300; `per_proxy_rate_sec` default 1.0; `concurrency` default 1.
- Proxy passwords only from env vars named by `password_env`; no proxy URL or password in any log line, outcome, exception text or MCP response. Group label only.
- MCP server: refuses private addresses unless `--allow-private`; never escalates past tier 2 (clamps a config's `max_tier: 3` and adds a note), because the redirect guard does not cover browser navigation; file reads and dataset paths confined to the working directory.
- All tests offline. Existing tests keep passing (90 at plan time).
- Commits: `git -c user.name=hermes -c user.email=hermes@karlsundstrom commit` with trailer `Co-authored-by: Karl Sundström <77639664+vhsgreed@users.noreply.github.com>`.
- After each task: update block 12 in `~/STATUS.md` (Current + Last touched).

## Review Focus

1. **Proxy credentials leaking through exception text.** httpx/curl_cffi errors can embed the proxy URL. Expected: every `error` outcome note and `[run] fetch error` line is passed through `redact()`. Test in Task 7.
2. **A huge `Retry-After`** (e.g. `86400`). Expected: the wait is capped at `cooldown_sec`. Test in Task 6.
3. **Config with more than one page source** (`paginate:` plus `pages:`, or `pages:` plus `urls:`), or a `pages.template` without `{n}`. Expected: config error, exit 1, message names the keys. Test in Task 5.
4. **Obfuscated private addresses** (`http://0x7f.1/`, `http://2130706433/`, `http://[::1]/`, `http://localhost./`, a public host redirecting to `http://169.254.169.254/`). Expected: all refused by netguard. Test in Task 4.
5. **Ctrl-C during a parallel run.** Expected: pending futures cancelled, process exits within a second, no hang on the executor. Test in Task 5 (simulate with a fetcher raising `KeyboardInterrupt`).

---

### Task 1: Rename to certainlyscraped, shim, alias

**Files:**
- Move: `scrapeforge/` -> `certainlyscraped/` (`git mv`)
- Create: `scrapeforge/__init__.py` (shim), `tests/test_shim.py`
- Modify: `pyproject.toml`, `certainlyscraped/__init__.py`, `certainlyscraped/fetch.py`, `certainlyscraped/cli.py` (`prog`), every `tests/*.py` import, `.github/workflows/*.yml`, `tests/test_user_agent.py`

**Interfaces:**
- Produces: package `certainlyscraped`; commands `certainlyscraped`, `cscrape` -> `certainlyscraped.cli:main`; `scrapeforge` -> `scrapeforge:_deprecated_main`; `certainlyscraped.fetch.USER_AGENT`.

- [ ] **Step 1: Write failing tests** in `tests/test_shim.py`:

```python
def test_old_import_warns_and_aliases():
    import importlib, sys, warnings
    sys.modules.pop("scrapeforge", None)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        sf = importlib.import_module("scrapeforge")
    assert any(issubclass(x.category, DeprecationWarning) for x in w)
    import certainlyscraped.paginate
    assert importlib.import_module("scrapeforge.paginate") is certainlyscraped.paginate
    assert sf.scrape is certainlyscraped.scrape

def test_old_command_prints_notice(capsys):
    import scrapeforge
    try:
        scrapeforge._deprecated_main(["probe", "--help"])
    except SystemExit:
        pass
    assert "renamed to certainlyscraped" in capsys.readouterr().err
```

and change `tests/test_user_agent.py` to expect `certainlyscraped/{__version__} ` and `github.com/vhsgreed/certainlyscraped`.

- [ ] **Step 2:** `sfv/bin/python -m pytest -q` -> FAIL (`No module named certainlyscraped`).
- [ ] **Step 3: Implement.** `git mv scrapeforge certainlyscraped`; rewrite `from scrapeforge`/`import scrapeforge` in tests to `certainlyscraped`; `__version__ = "0.4.0.dev0"`; `USER_AGENT` per Global Constraints; argparse `prog="certainlyscraped"`. Shim `scrapeforge/__init__.py`: `warnings.warn(..., DeprecationWarning, stacklevel=2)`, `from certainlyscraped import *` plus `__version__`, then for every module in `pkgutil.iter_modules(certainlyscraped.__path__)` set `sys.modules[f"scrapeforge.{name}"]`; `_deprecated_main(argv=None)` prints `scrapeforge was renamed to certainlyscraped; the old command goes away in 0.5.0` to stderr and calls `certainlyscraped.cli.main(argv)`. `pyproject.toml`: `name = "certainlyscraped"`, `packages = ["certainlyscraped", "scrapeforge"]`, version attr `certainlyscraped.__version__`, three `[project.scripts]`, Homepage URL to the new repo, `mcp = ["mcp>=2.3,<3"]` extra. Workflows: `scrapeforge run|init` -> `certainlyscraped`, release.yml version regex path. Delete stale `scrapeforge.egg-info`.
- [ ] **Step 4:** `sfv/bin/pip install -e ".[dev]" && sfv/bin/python -m pytest -q` -> all pass; `sfv/bin/cscrape --help` and `sfv/bin/scrapeforge --help` both exit 0, the latter with the notice.
- [ ] **Step 5:** Commit `refactor!: rename scrapeforge to certainlyscraped (0.4.0.dev0)`; push; confirm `tests` and `live` workflows green with `gh run list -R vhsgreed/scrapeforge --limit 3`.
- [ ] **Step 6:** `gh repo rename certainlyscraped -R vhsgreed/scrapeforge --yes`; `git remote set-url origin https://github.com/vhsgreed/certainlyscraped`; `mv ~/src/scrapeforge ~/src/certainlyscraped`; verify `curl -sI https://github.com/vhsgreed/scrapeforge` redirects (301) and the star count is unchanged.

### Task 2: PageFetch, PageOutcome, classification

**Files:**
- Create: `certainlyscraped/outcome.py`, `tests/test_outcome.py`

**Interfaces:**
- Produces:
  - `@dataclass PageFetch(status: int, html: str, attempts: int = 1, steps: list[str] = [], proxy_group: str | None = None, elapsed_ms: int = 0, note: str = "")`
  - `@dataclass PageOutcome(url, outcome, status, rows, attempts, steps, proxy_group, elapsed_ms, note)`; `to_dict() -> dict` with exactly the spec section 7 keys.
  - `@dataclass Verdict(outcome: str, note: str, challenge: bool)`
  - `classify_fetch(url: str, status: int, html: str) -> Verdict`: outcome in `ok | blocked | rate_limited | not_found | error` (never `empty`; the caller sets `empty` when an `ok` page yields 0 rows).
  - `INCOMPLETE = frozenset({"blocked", "rate_limited", "error"})`
  - `scrape_status(n_rows: int, outcomes: list[PageOutcome], min_rows: int) -> str`: one of `complete | incomplete | too_few_rows | blocked | error`.
  - `summary_line(outcomes) -> str`.

- [ ] **Step 1: Write failing tests** (parametrised `test_classify_fetch`):

| status, body | outcome | challenge |
|---|---|---|
| 200, books fixture | ok | False |
| 404, "" | not_found | False |
| 410, "" | not_found | False |
| 429, "slow down" | rate_limited | False |
| 419, "Sorry" | rate_limited | False |
| 403, "<title>Access Denied</title>" | blocked | True |
| 403, "nope" | blocked | False |
| 503, `challenges.cloudflare` page | blocked | True |
| 500, "oops" | error | False |
| 0, "" | error | False |
| 200, 5 kB body with `g-recaptcha` | blocked | True |

`challenge` is True exactly when `classify_response` found markers or a block pattern on a small body. Plus:
  - `test_status_complete`: 30 rows, outcomes `[ok, ok, not_found]`, min_rows 10 -> `complete`.
  - `test_status_incomplete`: 30 rows, `[ok, rate_limited]` -> `incomplete`.
  - `test_status_too_few_wins`: 3 rows, `[ok, blocked]`, min_rows 10 -> `too_few_rows`.
  - `test_status_blocked`: 0 rows, `[blocked]` -> `blocked`.
  - `test_status_error`: 0 rows, `[error]` -> `error`.
  - `test_empty_not_incomplete`: 30 rows, `[ok, empty, empty]` -> `complete`.
  - `test_summary_line`: `[ok]*9 + [blocked 419 ?p=2]` -> `"[done] 10 pages: 9 ok, 1 blocked (HTTP 419 on /news?p=2)"` (path+query of the first incomplete page).
- [ ] **Step 2:** run `tests/test_outcome.py` -> FAIL (module missing).
- [ ] **Step 3:** implement in `outcome.py`, delegating marker detection to `classify.classify_response`; status rules evaluated in this order: all outcomes `blocked`/`rate_limited` and 0 rows -> `blocked`; all `error` and 0 rows -> `error`; rows < min_rows -> `too_few_rows`; any in `INCOMPLETE` -> `incomplete`; else `complete`.
- [ ] **Step 4:** full suite passes.
- [ ] **Step 5:** commit `feat: page outcome classification`.

### Task 3: Outcomes through paginate, api and CLI; exit 4

**Files:**
- Modify: `certainlyscraped/paginate.py`, `api.py`, `cli.py`, `verify.py` (add `EXIT_INCOMPLETE = 4`)
- Test: `tests/test_paginate.py`, `tests/test_api.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 2.
- Produces:
  - `follow_links(fetch_page: Callable[[str], PageFetch], base_url: str, cfg: dict, on_page: Callable[[str, str], int], log=None) -> list[PageOutcome]` (on_page gets `(html, url)` and returns rows added).
  - `paginate(...)` kept with its current signature and int return (count of outcomes whose status < 400) for API compatibility; implemented on top of `follow_links`.
  - `as_page_fetcher(fetcher: Callable[[str], tuple[int, str]], tier: int) -> Callable[[str], PageFetch]` in `api.py` (steps `[f"t{tier}"]`, measures `elapsed_ms`).
  - `ScrapeResult.page_report: list[PageOutcome]`, `ScrapeResult.status(min_rows: int = 1) -> str`.
  - `exit_code(status: str, accept_partial: bool) -> int` in `cli.py`: complete 0, incomplete 4 (0 with accept_partial), too_few_rows 2, blocked 2, error 1.
  - CLI `run` flags `--report PATH` (JSONL of `to_dict()`), `--accept-partial`.

- [ ] **Step 1: Failing tests.**
  - `test_api.py::test_page_report_marks_419_rate_limited`: the existing `blocked` fetcher (419 on page2) -> `res.page_report[1].outcome == "rate_limited"`, `res.status() == "incomplete"`.
  - `test_api.py::test_done_line_counts_outcomes`: log contains `"[done] 2 pages: 1 ok, 1 rate_limited (HTTP 419 on /page2.html)"`.
  - `test_cli.py::test_run_exits_4_on_incomplete` and `..._accept_partial_exits_0`: monkeypatch `cli.scrape` to return a result with rows 5 and outcomes `[ok, blocked]`; `--min-rows 1` -> `SystemExit.code == 4`; with `--accept-partial` no SystemExit.
  - `test_cli.py::test_run_too_few_rows_beats_incomplete`: rows 1, `[ok, blocked]`, `--min-rows 5` -> 2.
  - `test_cli.py::test_report_jsonl`: `--report r.jsonl` writes one JSON object per page with the spec keys.
  - `test_paginate.py::test_follow_links_trailing_404_is_not_found`: outcomes `[ok, not_found]`.
  - Keep the two `[paginate] stopped:` log tests passing unchanged.
- [ ] **Step 2:** run -> FAIL.
- [ ] **Step 3:** implement; `[done]` line in `api.scrape` replaced by `summary_line`.
- [ ] **Step 4:** full suite passes; `sfv/bin/certainlyscraped run examples/hn-front.yaml --out /tmp/o --min-rows 60 --report /tmp/r.jsonl; echo $?` -> `0`, 10 lines in the report.
- [ ] **Step 5:** commit `feat: per-page outcomes, --report, exit 4 for incomplete runs`.

### Task 4: netguard and the MCP server

**Files:**
- Create: `certainlyscraped/netguard.py`, `certainlyscraped/mcp_server.py`, `tests/test_netguard.py`, `tests/test_mcp.py`
- Modify: `certainlyscraped/fetch.py` (guard hook), `cli.py` (`mcp` subcommand)

**Interfaces:**
- Produces:
  - `class PrivateAddressError(ValueError)`; `check_url(url: str, allow_private: bool = False, resolve=socket.getaddrinfo) -> None` (raises for loopback, RFC 1918, link-local, unique-local IPv6, unspecified, and any non-http(s) scheme; checks every address the host resolves to).
  - `fetch(url, tier, ..., guard: Callable[[str], None] | None = None)`: when `guard` is set, tiers 1 and 2 follow redirects manually (max 10 hops) calling `guard` on every hop; tier 3 raises `FetchError("tier 3 disabled under the MCP guard")`.
  - `build_server(workdir: Path, allow_private: bool = False, proxies: str | None = None) -> MCPServer` with tools `probe(url)`, `init(url)`, `try_selector(url, selector, within=None)`, `scrape(config, min_rows=1, max_rows=50)`. Each returns a dict; `scrape` returns `{"status", "rows", "row_count", "dataset_path", "pages": [PageOutcome.to_dict()...], "notes"}`, writing the full dataset to `workdir/.certainlyscraped/<name>-<UTC timestamp>.jsonl`.
  - `certainlyscraped mcp [--allow-private] [--proxies FILE] [--workdir DIR]` runs `build_server(...).run("stdio")`; if `mcp` is not installed, exit 1 with `pip install "certainlyscraped[mcp]"`.

- [ ] **Step 1: Failing tests.**
  - `test_netguard.py`: parametrised refusal of `http://127.0.0.1/`, `http://localhost./`, `http://0x7f.1/`, `http://2130706433/`, `http://10.0.0.1/`, `http://192.168.1.1/`, `http://169.254.169.254/latest/`, `http://[::1]/`, `http://[fd00::1]/`, `file:///etc/passwd`; acceptance of `https://93.184.215.14/` with an injected `resolve`; `allow_private=True` accepts `http://127.0.0.1/`.
  - `test_netguard.py::test_redirect_into_private_refused`: `httpx.MockTransport` public URL -> 302 to `http://169.254.169.254/` -> `fetch(..., guard=check_url)` raises `PrivateAddressError` (fetch takes an injectable `transport` kwarg for tier 1, test-only default None).
  - `test_mcp.py` (skip module if `mcp` missing), via `async with Client(build_server(tmp_path, allow_private=True)) as c` and `json.loads(result.content[0].text)`:
    - `test_tools_listed`: names == `{"probe", "init", "try_selector", "scrape"}`.
    - `test_scrape_inline_yaml_file_url`: config using `file://` on `tests/fixtures/books.html` copied into `tmp_path` -> `status == "complete"`, `len(rows) <= 50`, `row_count == 20`, `dataset_path` exists under `tmp_path`.
    - `test_scrape_refuses_private_by_default`: server without allow_private, config url `http://127.0.0.1:9/` -> `status == "error"`, note mentions `private address`.
    - `test_config_path_outside_workdir_refused`: `config="../x.yaml"` -> error status, no file read.
    - `test_max_tier_clamped`: config `max_tier: 3` -> notes contain `max_tier clamped to 2`.
  - `test_mcp.py::test_stdio_server_roundtrip`: `Client(StdioServerParameters(command=sys.executable, args=["-m", "certainlyscraped", "mcp", "--allow-private", "--workdir", str(tmp_path)]))`, call `try_selector` on the fixture file -> match count 20.
- [ ] **Step 2:** run -> FAIL.
- [ ] **Step 3:** implement. Tools are sync functions that call the existing API (`classify.probe`, `assist` helpers refactored to return data rather than print where needed, `api.scrape` with a guarded fetcher). `file://` URLs allowed only inside `workdir`.
- [ ] **Step 4:** full suite passes; manual check: register the server in Hermes (procedure in the hermes-agent skill), call `probe` on `https://books.toscrape.com/` from a chat, and confirm `tier: 1` comes back.
- [ ] **Step 5:** commit `feat: MCP server with private-address guard`.

### Task 5: pages: / urls: sources and the parallel runner

**Files:**
- Create: `certainlyscraped/runner.py`, `tests/test_runner.py`
- Modify: `api.py` (source selection), `examples/hn-pages.yaml` (new)

**Interfaces:**
- Consumes: `PageFetch`, `PageOutcome`, `classify_fetch` (Task 2).
- Produces:
  - `page_source(cfg: dict) -> tuple[str, list[str]]`: `("links", [url])`, `("range", urls)` or `("list", urls)`; raises `ValueError` naming the keys when more than one of `paginate`/`pages`/`urls` is set or `{n}` is missing.
  - `class RateGate(interval: float, clock=time.monotonic, sleep=time.sleep)`; `wait(key: tuple[str, str | None]) -> None`, thread-safe, one slot per key per interval.
  - `run_parallel(fetch_page, urls: list[str], cfg: dict, on_page, log=None, stop_on_trailing: bool = False) -> list[PageOutcome]`: `ThreadPoolExecutor(cfg.get("concurrency", 1))`; `on_page` called on the calling thread in page order; with `stop_on_trailing`, after two consecutive `not_found`/`empty` in page order, cancels remaining futures and drops later results.

- [ ] **Step 1: Failing tests.**
  - `test_rows_in_page_order_under_random_latency`: 8 pages, fetcher sleeps `random.uniform(0, 0.05)`, concurrency 4 -> on_page call order == URL order, 3 runs identical output.
  - `test_rate_gate_per_host`: fake clock, interval 1.0, 3 waits on `("h", None)` -> recorded times `[0, 1, 2]`; waits on `("h", "a")` and `("h", "b")` both at 0.
  - `test_range_stops_after_two_trailing_not_found`: pages 1-10, 4-10 return 404 -> outcomes length 5 (1-3 ok, 4-5 not_found), fetches of page 6+ either not started or discarded.
  - `test_list_does_not_stop_on_not_found`: urls list with a 404 in the middle -> all fetched.
  - Review Focus 3: `test_two_sources_is_config_error`, `test_template_without_n_is_config_error`.
  - Review Focus 5: `test_keyboard_interrupt_cancels_pending`: fetcher raises `KeyboardInterrupt` on page 2 of 20, concurrency 4 -> `KeyboardInterrupt` propagates within 1 s, fewer than 20 fetches started.
- [ ] **Step 2:** run -> FAIL.
- [ ] **Step 3:** implement; `api.scrape` uses `follow_links` for `links` and `run_parallel` for `range`/`list`; the rate gate key is `(host, proxy_group)` from the PageFetch side (the ladder calls `gate.wait` before each attempt; until Task 6 the plain fetcher calls it).
- [ ] **Step 4:** full suite passes; `certainlyscraped run examples/hn-pages.yaml --out /tmp/o --min-rows 60` exits 0 (template `https://news.ycombinator.com/news?p={n}`, 1-4, concurrency 2, rate 1.0). Add it to the live workflow matrix with `min_rows: 90`.
- [ ] **Step 5:** commit `feat: pages/urls sources with ordered parallel fetching`.

### Task 6: Escalation ladder

**Files:**
- Create: `certainlyscraped/escalate.py`, `tests/test_escalate.py`
- Modify: `fetch.py` (`tier3_available() -> bool`), `api.py` (use the ladder as the default fetcher)

**Interfaces:**
- Consumes: `classify_fetch`, `PageFetch`, `RateGate`; `ProxyPool` protocol (Task 7) as `pool: Any | None` with `acquire(host)`, `alternate(host, exclude_groups)`, `report(proxy, host, outcome)`.
- Produces: `class Ladder(cfg: dict, start_tier: int, pool=None, gate: RateGate | None = None, fetch_fn=fetch.fetch, sleep=time.sleep, guard=None)`; `fetch_page(url: str) -> PageFetch`; `level(host) -> int`.

- [ ] **Step 1: Failing tests** (fake `fetch_fn(url, tier, proxy=..., ...)` scripted per call; fake `sleep` recording waits):
  - `test_ok_first_try`: steps `["t1"]`, attempts 1.
  - `test_rate_limited_without_pool_backs_off_once`: 429 then 200, header-free -> waits `[cooldown_sec]`, steps `["t1", "t1"]`.
  - `test_retry_after_honoured` and Review Focus 2 `test_retry_after_capped`: `Retry-After: 7` -> wait 7; `Retry-After: 86400`, cooldown 300 -> wait 300. (`fetch_fn` returns headers via a third tuple element; `fetch.fetch` gains `return_headers=False`.)
  - `test_challenge_escalates_tier`: Cloudflare challenge at t1, ok at t2 -> steps `["t1", "t2"]`.
  - `test_plain_403_without_pool_escalates`: steps `["t1", "t2"]`.
  - `test_sticky_level`: after escalation on host h, next `fetch_page` on h starts at t2; a different host starts at t1.
  - `test_max_tier_caps`: challenge at t1 and t2, max_tier 2 -> outcome stays blocked, steps `["t1", "t2"]`, note contains `max_tier 2 reached`.
  - `test_tier3_not_installed`: max_tier 3, `tier3_available` False -> note `tier 3 not available`.
  - `test_max_retries`: always 429, max_retries 3 -> attempts 4.
- [ ] **Step 2:** run -> FAIL.
- [ ] **Step 3:** implement the rules of spec section 4 in that order; each step string is `t<tier>` or `t<tier>@<group>`.
- [ ] **Step 4:** full suite passes; live examples still exit 0.
- [ ] **Step 5:** commit `feat: per-site escalation ladder`.

### Task 7: Proxy pool

**Files:**
- Create: `certainlyscraped/proxypool.py`, `tests/test_proxypool.py`, `tests/fixtures/proxies.txt`
- Modify: `fetch.py` (accept `Proxy` objects; `redact(text: str, secrets: list[str]) -> str`), `api.py`, `escalate.py`, `cli.py` (redact fetch-error line), `mcp_server.py` (`--proxies`)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Proxy(url: str, group: str)` whose `__repr__`/`__str__` show only `Proxy(group=...)`.
  - `class ProxyPool.from_config(cfg: dict, env=os.environ, base_dir=".") -> ProxyPool | None` (None when no `proxy`/`proxies`; a bare `proxy:` string -> pool of one in group `default`).
  - `acquire(host: str) -> Proxy | None`, `alternate(host, exclude_groups: set[str]) -> Proxy | None`, `report(proxy, host, outcome: str) -> None`, `secrets() -> list[str]`.
  - Gateway sessions: `rotate` -> new `secrets.token_hex(4)` per acquire; `sticky` -> one token per (host, thread id).

- [ ] **Step 1: Failing tests.**
  - `test_groups_from_labels_and_prefix`: list lines `http://u:p@10.1.2.3:8000 # dc-a`, `http://10.1.9.9:8000`, `http://10.2.0.1:8000`, `http://[2001:db8:1::5]:8000`, `http://proxy.example:3128` -> groups `dc-a`, `10.1/16`, `10.2/16`, `2001:db8:1::/48`, `proxy.example`.
  - `test_cooldown_is_per_host`: report blocked on (p1, a.com) -> `acquire("a.com")` never returns p1 for `cooldown_sec` (fake clock), `acquire("b.com")` can.
  - `test_alternate_skips_tried_groups`.
  - `test_gateway_template_and_env_password`: env `GATEWAY_PASSWORD=s3cret` -> URL filled; missing env var -> `ValueError` naming the variable, not the value.
  - `test_ladder_switches_group_on_rate_limit`: pool with groups a, b; 429 via a, 200 via b -> steps `["t1@a", "t1@b"]`, `proxy_group == "b"`.
  - `test_plain_403_tries_other_group_before_tier`: steps `["t1@a", "t1@b", "t2@b"]` when both groups 403 at t1.
  - Review Focus 1 `test_no_secret_anywhere`: a run whose fetch raises `httpx.ProxyError("http://u:s3cret@10.1.2.3:8000 refused")` -> `s3cret` and `10.1.2.3` absent from the log, every `to_dict()`, `--report` file and the MCP `scrape` response.
- [ ] **Step 2:** run -> FAIL.
- [ ] **Step 3:** implement; one `threading.Lock` around pool state; `per_proxy_rate_sec` feeds the `RateGate` key `(host, group)`.
- [ ] **Step 4:** full suite passes.
- [ ] **Step 5:** commit `feat: proxy pool with per-host cooldown and group rotation`.

### Task 8: README, docs, release

**Files:**
- Modify: `README.md`, `docs/selectors.md`, `VERIFIED-SITES.md`, `schema/*.json` (add page report schema), `.github/workflows/live.yml`, `certainlyscraped/__init__.py` (`0.4.0`)

- [ ] **Step 1:** README per spec section 9: headline "A hybrid scraper with escalating effort.", the subline, MCP setup snippet (`pip install "certainlyscraped[mcp]"` + a client config block with `"command": "certainlyscraped", "args": ["mcp"]`) within the first screen; ladder table; proxies section stating the single-host-no-pool speed limit; reworded "What it will not do"; exit code table with 4; old-name note.
- [ ] **Step 2:** verify every command in the README runs: extract fenced `bash` blocks that start with `certainlyscraped`/`cscrape`, run them against fixtures or the live sandbox sites, all exit 0.
- [ ] **Step 3:** live workflow: each job asserts exit code 0 explicitly (`test $? -eq 0`, so a 4 fails the job and shows up as a red check).
- [ ] **Step 4:** bump to `0.4.0`, commit `release: certainlyscraped 0.4.0`, tag `v0.4.0`, push; `release` workflow green; dist artifact present.
- [ ] **Step 5:** hand Karl the PyPI trusted-publisher steps (pending publisher for `vhsgreed/certainlyscraped`, workflow `release.yml`, environment `pypi`, then repo variable `PYPI_PUBLISH=true`). Do not publish without him.
- [ ] **Step 6:** record stars in `~/STATUS.md` (checkpoint after Task 4 too).
