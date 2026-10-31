# certainlyscraped 0.4.0: design

Status: approved by Karl 2026-10-31. Evolves scrapeforge 0.3.0 in place.

## 1. Goal and viability

- **Headline:** "A hybrid scraper with escalating effort."
- **Subline:** it starts with the cheapest fetch that works, escalates only when a page is
  blocked, and reports exactly what it got and why it stopped.
- **Audience:** AI agents and the people who run them. The agent interface (MCP) and
  machine-readable outcomes are first-class; the CLI stays for humans and scripts.
- **Viability:** 150 GitHub stars on `vhsgreed/certainlyscraped` by 2027-01-31 (1 star on
  2026-10-31). Well under that on the date means the approach failed: stop adding
  features and decide whether to keep maintaining it.

## 2. Non-goals

- Logging in, login-gated content, or credentials that are not the user's.
- Beating fingerprint and JavaScript challenges (Cloudflare managed challenge,
  PerimeterX, DataDome) with proxies. Those pages report `blocked`. Tier 3 stays "no
  promise".
- Named proxy-provider presets (untestable without paid accounts). Added later, one at a
  time, only with a working test from the person who asks.
- Async rewrite, distributed workers, whole-site crawling, proxy scoring, free proxy
  list scraping, a built-in proxy checker.
- Shipping scraped data.

## 3. Naming

- Repo `vhsgreed/scrapeforge` renamed to `vhsgreed/certainlyscraped` (GitHub redirects old
  URLs; star and history stay).
- Python package, import name and command: `certainlyscraped`. Short alias command:
  `cscrape` (free on PyPI and not on PATH, checked 2026-10-31).
- `scrapeforge` command and `import scrapeforge` keep working through 0.4.x with a
  deprecation notice on stderr; removed in 0.5.0.
- User-Agent: `certainlyscraped/<version> (+https://github.com/vhsgreed/certainlyscraped)`.
- Version: 0.4.0.

## 4. Escalation ladder (the headline feature)

Today the probe picks one tier per job. 0.4.0 escalates per site when a page comes
back blocked:

1. **Start** at the probed tier (or the config's `tier`).
2. **On `rate_limited`** (429, or 419 without challenge markers): retry the page through
   a proxy from a different group, if a pool is configured; otherwise back off
   (`retry_after` header, else `cooldown_sec`) and retry once.
3. **On `blocked`** with challenge markers (classify_response found a challenge page):
   raise the tier by one, up to `max_tier`.
4. **On `blocked`** without markers (plain 403): try a different proxy group first if
   any remain, then raise the tier.
5. **Sticky:** once a site escalates on a challenge or a 403, later pages on that site
   start at the new level. No re-probing per page. A level reached only by an error
   climb (a 5xx or no response) is not remembered.
6. `max_tier` defaults to 2. Tier 3 is used only if `max_tier: 3` and the tier3 extra is
   installed; otherwise the page ends `blocked` with a note saying tier 3 was not
   available.
7. Every escalation step is one attempt, counted against `max_retries` (default 3).
8. **On a status-0 error (no response):** with a pool, try one other proxy group once
   (the proxy is the likely fault), then retry the same tier once, then climb. A 5xx
   is a response and keeps the lease. A `PrivateAddressError` from the guard is
   terminal: one attempt, no retry, no climb.

Each page outcome (section 7) records the steps taken and the leased group, e.g.
`["t1@dc-a", "t1@dc-b", "t2@dc-b"]` (a 403 from one group, a challenge from the other,
real HTML at tier 2). With a pool every step carries `@group`; with no pool none does.

## 5. Proxy pool

### Config

The single `proxy:` string still works and becomes a pool of one.

```yaml
proxies:
  list: proxies.txt          # one proxy URL per line, optional "# group" label
  gateway:                   # optional generic provider gateway, alongside or instead
    url: "http://{user}-country-{country}-session-{session}:{password}@gw.example:7000"
    user: myuser
    password_env: GATEWAY_PASSWORD
    country: se
    session: rotate          # or "sticky"
  per_proxy_rate_sec: 1.0
  cooldown_sec: 300
  max_retries: 3
```

### Behaviour (`proxypool.py`)

- `acquire(host) -> Proxy | None`: picks a proxy not cooling down for `host`; returns
  None immediately when every group is cooling down for that host. It does not block:
  pacing is the ladder's rate gate, not the pool's.
- `report(proxy, host, outcome)`: `ok` resets; `rate_limited`, `error` and a `blocked`
  **without** challenge markers put that proxy in cooldown for that host only. A
  challenge parks nothing: the proxy reached the site, so the site is the problem.
- `alternate(host, exclude_groups) -> Proxy | None`: a proxy from a group not yet tried
  for this page.
- **Groups:** gateway proxies form one group per gateway; list proxies use their label,
  else a neutral `net-1`, `net-2`, ... assigned in order of first appearance, one per
  /16 (IPv4), /48 (IPv6) or hostname. The derived network or hostname is an internal
  key only: no address or hostname reaches `steps` or `proxy_group`.
- `session: rotate` generates a new random session token per lease (one per page; a
  403/429 group switch leases again); `sticky` keeps one per (host, worker).
- **Never without a proxy (R27, corrected by R33):** when a pool is configured, a page is
  fetched through a proxy or not at all. If `acquire` returns None at the start of the
  page (every group is cooling for that host), the page stops with outcome `blocked` and
  the note `no proxy available for <host>`; a verdict already reached is kept. There is
  no fallback to the origin IP.
- **`tier: auto` under a pool (R40):** `auto` does not probe when a pool is configured
  (a probe is a direct fetch). The ladder starts at tier 1 and climbs, so the first
  request already leaves through a proxy. Without a pool the probe is unchanged.
- **One proxy per page (R33, amended by R45):** a page *leases* the proxy `acquire` gave
  it for its whole life. A network-level verdict (a 429, or a 403 without challenge
  markers) tries `alternate` once and moves the lease to the new group if one is free;
  with none left the page keeps the proxy it holds and follows the normal rule (back off
  once and retry, or climb the tier). A challenge, a 5xx and every climb keep the current
  proxy: the tier is what is changing, not the endpoint. A status-0 error (no response)
  is different: it tries `alternate` once before the same-tier retry, and a tier reached
  only by an error climb is not remembered. A park earned by this page still applies to
  the *next* page on that host, never to the page that is already running; otherwise the
  first wall would end the ladder.
- **Secrets:** passwords only from environment variables named in the config, or from a
  dedicated proxies file (`--proxies`, `CERTAINLYSCRAPED_PROXIES`). Every log line, page
  outcome and MCP response shows the group label, never the URL, host or password --
  `scrub()` masks the bare host and `host:port` too, because the private-address guard
  reports `private address: 10.1.2.3` with no scheme (R32a).
- **No passwords in the main config (R31):** a userinfo password in `proxy:`/`proxies:`
  inside the YAML the user commits is refused with an error naming the alternative
  ("move it to a proxies file passed with `--proxies`, or use a gateway template with
  `password_env`"). A proxies file and the environment are the dedicated channels and may
  carry one; it is redacted everywhere from then on. The reason is the commit: a config
  that has to be shared, reviewed and versioned never carries a live password.
- Thread-safe (one lock); the pool is shared by the parallel fetcher.

## 6. Parallel fetching

```yaml
pages: { template: "https://news.ycombinator.com/news?p={n}", from: 1, to: 10 }
# or
urls: ["https://example.com/a", "https://example.com/b"]
concurrency: 4               # default 1 = today's behaviour
```

- `paginate:` (next-link) stays sequential; `pages:` and `urls:` run on a
  `ThreadPoolExecutor` with `concurrency` workers.
- `rate_limit_sec` (default 1.0; the ladder's `per_proxy_rate_sec` when set) applies per
  (host, proxy), so N proxies to one host give N slots. With no pool, one host gets at
  most one request per interval no matter the concurrency. Measured: eight pages at
  1.0 s on one host took 7.1 s with one proxy and 1.1 s with four, whether the four were
  one derived `net-1` group or four labelled groups. The docs say so plainly: speed
  comes from many hosts or a proxy pool, not from threads alone.
- Rows are emitted in page order regardless of completion order, so dedupe and output
  are deterministic.
- A `pages:` range stops early after any 2 consecutive `not_found` or `empty` pages,
  not only at the end of the range: a gap in the middle ends the run early. Those pages
  are not incomplete (section 7), so the run can still exit 0.

## 7. Page outcomes and exit codes

One record per page:

| Field | Example |
|---|---|
| `url` | `https://news.ycombinator.com/news?p=2` |
| `outcome` | `ok`, `empty`, `blocked`, `rate_limited`, `not_found`, `error` |
| `status` | `419` |
| `rows` | `0` |
| `attempts` | `2` |
| `steps` | `["t1", "t2"]` |
| `proxy_group` | `dc-a` or null |
| `elapsed_ms` | `840` |
| `note` | `challenge page: cloudflare` |

- Outcomes come from `classify_response` only, so "blocked" means one thing everywhere.
- `--report report.jsonl` writes the records; `ScrapeResult.page_report` returns them.
- Summary line: `[done] 10 pages: 9 ok, 1 blocked (HTTP 419 on /news?p=2)`, or
  `(no response on <path>)` when the first incomplete page never got a response.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | complete: at least `--min-rows`, no incomplete page |
| 1 | config, I/O or fetch error; with `--min-rows 0`, a run whose every page errored |
| 2 | fewer than `--min-rows` rows (an all-error run at the default `--min-rows 1`) |
| 3 | `probe --fail-blocked`: target is walled |
| 4 | **incomplete**: at least `--min-rows`, but one or more pages ended `blocked`, `rate_limited` or `error` after all retries |

- **Narrow on purpose** so 4 never becomes the normal result: trailing `not_found` /
  `empty` past the end of a range, and pages that succeeded on retry, do not count.
- `--accept-partial` maps 4 to 0 for scripts that only check for 0.
- 2 wins over 4 (too few rows is the bigger problem).
- **Measured:** the daily live check asserts exit 0 (not 4) on the healthy example sites.
  Any 4 there means the definition is too loose and gets fixed.

## 8. MCP server

- Optional extra: `pip install "certainlyscraped[mcp]"` (official `mcp` SDK, >=2.3);
  started with `certainlyscraped mcp` (stdio transport).
- Tools, each a thin call into the existing API:

| Tool | Returns |
|---|---|
| `probe(url)` | tier verdict, block markers, embedded JSON found |
| `init(url)` | config YAML + field coverage |
| `try_selector(url, selector, within?)` | match count, sample values, hints |
| `scrape(config, min_rows?, max_rows?)` | `status`, rows (up to `max_rows`, default 50), total row count, file path to the full dataset, page outcomes |

- `status` is one of `complete`, `incomplete`, `too_few_rows`, `blocked`, `error`, with
  the same meaning as exit codes 0, 4, 2, 3, 1.
- `config` is inline YAML or JSON text; a path is refused. `proxy`, `proxies`,
  `cookies`, `since`, `snapshot` and `fetcher` are refused, and so are unknown keys:
  `json` and `headers` are not config keys (`items.json` is where embedded JSON goes),
  and an agent sending one gets a status dict, not a silent no-op.
- **Private addresses refused by default:** loopback, RFC 1918, link-local (incl.
  169.254.169.254), and unique-local IPv6, checked after DNS resolution and on every
  redirect hop. `--allow-private` turns it off. A refusal is terminal for the page: one
  attempt, no retry, no climb, outcome `error` with the refusal in the note. File reads
  and the dataset path are confined to the working directory.
- Proxies come only from the server's own `--proxies proxies.yaml`, never from a tool
  argument and never from `CERTAINLYSCRAPED_PROXIES`. `probe`, `init` and `try_selector`
  lease one proxy from that pool per call; if none is free they return `blocked` with
  the note `no proxy available for <host>` and make no request. Without `--proxies` the
  server has no pool and fetches directly.
- The CLI does not refuse private addresses (a human scraping their own localhost is
  normal); only the MCP server does.

## 9. README

- Headline and subline from section 1; MCP setup snippet within the first screen.
- The tier table gains the escalation ladder and the proxy pool.
- "What it will not do" reworded: proxies the user brings get past IP bans, IP-range
  bans and rate limits; they do not beat fingerprint or JavaScript challenges, which
  report `blocked`.
- Unchanged: no logins, no promise a site works today, ships the tool not the data.

## 10. Module changes

| Module | Change |
|---|---|
| `fetch.py` | `fetch(url, tier, proxy=Proxy \| None, ...)`; `USER_AGENT` renamed |
| `proxypool.py` | new (section 5) |
| `escalate.py` | new: the ladder (section 4), per-host sticky level |
| `outcome.py` | new: `PageOutcome`, mapping from `classify_response` |
| `paginate.py` | returns `PageOutcome`s; `pages:` / `urls:` sources; parallel runner |
| `api.py` | `ScrapeResult.page_report`, `status`; wires pool + ladder |
| `cli.py` | exit 4, `--report`, `--accept-partial`, `mcp` subcommand, `cscrape` alias |
| `mcp_server.py` | new (section 8) |
| `netguard.py` | new: private-address check used by the MCP server |
| `scrapeforge/` | shim package re-exporting `certainlyscraped` with a deprecation warning |

## 11. Testing

- All new behaviour tested offline with injected fetchers; the existing 90 tests keep
  passing.
- Pool: rotation, per-host cooldown, group switching, secret redaction in every output.
- Ladder: each outcome-to-step rule, stickiness, `max_tier` cap, tier 3 not installed.
- Parallel: page order under random completion, per-(host, proxy) pace from recorded
  timestamps, early stop at the end of a range.
- Exit codes: every row of the table, plus `--accept-partial`.
- MCP: tools called in-process; one test starts the real stdio server and calls `probe`
  against a local fixture with `--allow-private`; netguard rejects each private range
  and a redirect into one.
- Live check: unchanged sites, now asserting exit 0; one `pages:` template job on HN.

## 12. Build order

1. Rename + shim + alias (0.4.0.dev0); CI green under the new name.
2. Page outcomes + exit 4 + `--report`.
3. MCP server + netguard.
4. `pages:` / `urls:` + parallel runner.
5. Escalation ladder.
6. Proxy pool (list + generic gateway).
7. README; tag 0.4.0; PyPI publish once Karl has set up trusted publishing.

Each step is shippable on its own; stars are checked against the target after step 3.

## 13. Parked (not in this spec or its plan)

- Launch posts, demo GIF re-record, awesome-list submissions, named provider presets.
- **Tier 4 (experimental, own spec after 0.4.0 ships):** a headful browser driven in the
  user's own logged-in session, for sites behind a login (Facebook, Reddit, Instagram).
  Constraints carried into that spec: the user logs in by hand in a dedicated browser
  profile and the tool never stores or types credentials; opt-in per run; low fixed rate;
  only what that account can already see; README states the account-ban and terms-of-
  service risk plainly. Section 2's "no logins" holds for tiers 1-3 and the MCP server.
  Karl's direction (2026-10-31): the novelty is not "includes a browser" but a library of
  per-site collection scripts in the repo that users contribute to, wired into the
  escalation ladder as its top rung with the ban warning. Scope: about 3 popular sites.
  Auth: a logged-in session, or username + password supplied by the user. Open question
  for that spec: storing/typing passwords vs session-only (the constraint above).

## 14. Decisions confirmed

- MCP server refuses private addresses by default (section 8). Approved with the spec.
- `cscrape` as the alias. Approved by Karl 2026-10-31.
- Old `scrapeforge` names removed in 0.5.0. Approved with the spec.
