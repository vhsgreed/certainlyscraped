# certainlyscraped

A hybrid scraper with escalating effort.

It starts with the cheapest fetch that works, escalates only when a page is
blocked or still failing, and reports exactly what it got and why it stopped.
It reads the page, writes a config for you, checks that every field filled, and
turns a listing into rows instead of a promise.

[![tests](https://github.com/vhsgreed/certainlyscraped/actions/workflows/tests.yml/badge.svg)](https://github.com/vhsgreed/certainlyscraped/actions/workflows/tests.yml)
[![live check](https://github.com/vhsgreed/certainlyscraped/actions/workflows/live.yml/badge.svg)](https://github.com/vhsgreed/certainlyscraped/actions/workflows/live.yml)

## Quickstart: 20 seconds

```bash
pip install "certainlyscraped[mcp]"

cscrape init https://books.toscrape.com/catalogue/page-1.html --name books
cscrape run books.yaml
```

`init` probes the site, finds the block that repeats on the page, guesses the
fields and the next-page link, and prints how many items each field filled.
`run` fetches the pages, writes `out.csv`, and fails loudly if it gets fewer
rows than `--min-rows` (default 1) instead of reporting success.

For an agent, the same install already includes the MCP server:

```bash
pip install "certainlyscraped[mcp]"
certainlyscraped mcp
```

```json
{
  "mcpServers": {
    "certainlyscraped": {
      "command": "certainlyscraped",
      "args": ["mcp"]
    }
  }
}
```

That gives the agent `probe`, `init`, `try_selector` and `scrape`; the full
contract is under [MCP server (for agents)](#mcp-server-for-agents).

![certainlyscraped demo recorded before the rename: init, try, run](docs/demo.gif)

Real output from the daily live check:

```
[probe] https://books.toscrape.com/catalogue/page-1.html -> tier 1 (static page)
[init] wrote books.yaml: 20 items on this page (20 repeats, median text 51 chars, each has a link, each has an image)
field coverage on this page:
  title           20/20 (100%)
  url             20/20 (100%)
  image           20/20 (100%)
  price           20/20 (100%)
  availability    20/20 (100%)
```

The config is plain YAML you can read and edit:

```yaml
name: books
url: "https://books.toscrape.com/catalogue/page-1.html"
tier: 1
max_pages: 5
rate_limit_sec: 2.0
paginate:
  selector: "li.next a"
items:
  selector: "article.product_pod"
fields:
  title:        { selector: "h3 a", attr: title }
  url:          { selector: "h3 a", attr: href, absolute: true }
  image:        { selector: "img.thumbnail", attr: src, absolute: true }
  price:        { selector: "p.price_color", attr: text, type: float }
  availability: { selector: "p.instock.availability", attr: text }
output: csv
```

## The escalation ladder

Every page starts at the probed tier (or the config's `tier`) and climbs only
when that rung fails.

| What came back | What happens next |
|---|---|
| rate limited (429, or 419 without a challenge) | try the page through another proxy group; if there is none, wait once (`Retry-After`, else `cooldown_sec`) and retry. Prints `[ladder] <host>: rate limited, waiting <n>s` |
| challenge page (Cloudflare, PerimeterX, DataDome markers) | keep the proxy, raise the tier by one, up to `max_tier` |
| plain 403 | try another proxy group first; with none left, raise the tier |
| 5xx | an error page keeps the proxy: retry once at the same tier, then raise the tier. A short 503 (body under 20 kB) is classified as a block instead, not an error, so it gets no same-tier retry: it buys another proxy group when one exists, otherwise it raises the tier |
| no response (refused, DNS, TLS, reset) | with a pool, try another proxy group once; then, pool or not, retry once at the same tier, then raise the tier |

Every group switch also prints `[ladder] <host>: <reason>, trying another proxy
group`, so a rotation is visible while it happens.

Once a site escalates on a challenge or a 403, later pages on that site start
at the new level. A level reached only by an error climb is not remembered.
`steps` in the page report shows the rungs and the leased group, for example
`["t1@dc-a", "t1@dc-b", "t2@dc-b"]` (a 403 from one group, a challenge from the
other, real HTML at tier 2).

### Tiers

| Tier | What it is | What it costs |
|---|---|---|
| 1 | `httpx`, plain HTTP | milliseconds per page, almost no install |
| 2 | `curl_cffi`, TLS impersonation | a real Python dependency; a site that rejects Python's TLS fingerprint may still serve it real HTML |
| 3 | a browser stack (`scrapling`, `cloakbrowser` or `playwright`) | an optional, heavy extra; used only with `max_tier: 3` and never promised for a specific site |

`tier: auto` probes first and picks the lowest tier that returns a real page.
With a proxy pool configured, `auto` skips the probe and starts at tier 1: a
probe is a direct request, and with a pool no request may leave from the origin
IP. The ladder climbs from there. A tier is not a promise: it is where the
ladder starts.

## Outcomes and exit codes

One record per page, available with `--report report.jsonl` and in the MCP
response's `pages` field:

```json
{"url": "https://httpbin.org/status/403", "outcome": "blocked", "status": 403, "rows": 0, "attempts": 2, "steps": ["t1", "t2"], "proxy_group": null, "elapsed_ms": 1358, "note": "max_tier 2 reached"}
```

`outcome` is one of `ok`, `empty`, `blocked`, `rate_limited`, `not_found`,
`error`. A run also prints one summary line:

```
[done] 2 pages: 1 ok, 1 blocked (HTTP 403 on /status/403)
```

A page with no response has no HTTP status to quote, so it reads
`[done] 1 page: 1 error (no response on /list)`.

| Exit code | Meaning |
|---|---|
| 0 | complete: at least `--min-rows`, no incomplete page |
| 1 | config, I/O or fetch error; with `--min-rows 0`, a run whose every page errored |
| 2 | fewer than `--min-rows` rows (2 wins over 4); an all-error run at the default `--min-rows 1` has no rows, so it is 2 |
| 3 | `probe --fail-blocked`: the target is walled |
| 4 | incomplete: at least `--min-rows`, but one or more pages ended `blocked`, `rate_limited` or `error` after all retries |

Exit 1 for a page-level failure is reachable only when `--min-rows 0` asks for
no rows. At the default `--min-rows 1`, a run that lost every page to errors
fails the row check first and exits 2.

Exit 4 is narrow on purpose. A trailing `not_found` or `empty` page past the
end of a range does not count, and neither does a page that succeeded on retry.
`--accept-partial` maps 4 to 0 for scripts that only check for zero. A run that
loses one page still writes the rows it did get, and the report says which page
is missing.

## MCP server (for agents)

```bash
pip install "certainlyscraped[mcp]"
certainlyscraped mcp
```

Client configuration:

```json
{
  "mcpServers": {
    "certainlyscraped": {
      "command": "certainlyscraped",
      "args": ["mcp"]
    }
  }
}
```

Four tools, each a thin call into the same API:

| Tool | Returns |
|---|---|
| `probe(url)` | tier verdict, block markers, embedded JSON found |
| `init(url)` | config YAML plus field coverage |
| `try_selector(url, selector, within?)` | match count, sample values, hints |
| `scrape(config, min_rows?, max_rows?)` | `status`, rows (up to `max_rows`, default 50), the true row count, a dataset path, and per-page outcomes |

`config` is inline YAML or JSON text, never a path. Config keys `proxy`,
`proxies`, `cookies`, `since`, `snapshot` and `fetcher` are refused, and so are
unknown keys such as `json` and `headers` (embedded JSON goes under
`items.json`). `status` says what the run
got: `complete` (at least `min_rows` rows and no incomplete page), `incomplete`
(enough rows, but a page ended blocked, rate-limited or errored),
`too_few_rows` (fewer than `min_rows` rows), `blocked` (zero rows and every
page was walled or rate-limited) and `error` (zero rows and every page
errored). These are not the CLI's exit codes: a blocked-only `run` exits 2,
exit 3 is only `probe --fail-blocked`, and exit 1 is a config, I/O or fetch
exception (or, with `--min-rows 0`, an all-error run). A bad request is still a
status dict: a tool never raises into the JSON-RPC channel.

Private addresses are refused by default: loopback, RFC 1918, link-local
(including `169.254.169.254`) and unique-local IPv6, checked after DNS
resolution and on every redirect hop. `--allow-private` turns that off for
local development. The CLI does not refuse private addresses; only the MCP
server does. A refusal is terminal for the page: one attempt, no retry, no
climb, outcome `error` with the refusal in the note.

Proxies come only from the server's own `--proxies proxies.yaml`, never from a
tool argument, and the server never reads `CERTAINLYSCRAPED_PROXIES`. `probe`,
`init` and `try_selector` lease one proxy from that pool for the call; if the
pool has nothing free they return `blocked` with `no proxy available for
<host>` and make no request. Without `--proxies` the server has no pool and
fetches directly. File reads and datasets stay inside the working directory.

## Proxies

One proxy still works and becomes a pool of one:

```yaml
proxy: "http://10.1.2.3:8000"
```

A pool names groups so a 403 or a 429 can try a different network before paying
for a heavier tier:

```yaml
proxies:
  list: proxies.txt          # one URL per line, optional "# group" label
  gateway:                   # one generic provider gateway
    template: "http://{user}-country-{country}-session-{session}:{password}@gw.example:7000"
    user: myuser
    password_env: GATEWAY_PASSWORD
    country: se
    session: rotate          # or sticky
  per_proxy_rate_sec: 1.0
  cooldown_sec: 300
  max_retries: 3
```

`proxies.txt`:

```
http://user:pass@10.1.2.3:8000 # dc-a
http://10.1.9.9:8000           # dc-b
http://10.1.1.7:8000
```

Groups are the label after `#`, else a neutral label `net-1`, `net-2`, ... in
order of first appearance, one per IPv4 `/16`, IPv6 `/48` or hostname. A label
never contains a proxy address or hostname.
A proxy that is walled for one host is parked for that host for
`cooldown_sec`; it is not removed from the pool. `session: rotate` generates a
new token per lease: one per page, and a 403 or 429 that switches group leases
again. `sticky` keeps one token per host and worker.

Passwords never belong in the main config. Put them in a dedicated proxies
file passed with `--proxies`, or in the environment through `password_env`.
A password inside an inline `proxy:`/`proxies:` URL in the committed YAML is
refused with a message naming both alternatives. Page outcomes and MCP
responses carry the group label, never the proxy URL or password. A page that
failed on a dead proxy keeps the transport error as its note, for example
`ConnectionError: Failed to perform, curl: (7) Failed to connect to
books.toscrape.com:443 over proxy *** ...` (the target is kept, the proxy
address is `***`); an unresolvable host reads
`DNSError: ... Could not resolve host: no-such-host.invalid ...`. No log line
quotes a proxy address, so the group label and the error text are the
diagnosis. A private address refused by the MCP guard is terminal: one
attempt, no retry, no climb.

Precedence for `run`: `--no-proxy` > `--proxies FILE` >
`CERTAINLYSCRAPED_PROXIES` > the config. `--no-proxy` ignores every configured
proxy and fetches directly. MCP has no such flag, never reads
`CERTAINLYSCRAPED_PROXIES`, and without `--proxies` has no pool at all.

Speed comes from many hosts or a proxy pool, not from threads alone. The ladder
paces per (host, proxy) at `per_proxy_rate_sec` (it falls back to
`rate_limit_sec`; default 1.0).
With no pool, one host gets at most one request per interval no matter the
`concurrency`, so a single-host `pages:` job is still paced by the site. With a
pool, N proxies to one host give N slots: eight pages at 1.0 s on one host took
7.1 s with one proxy and 1.1 s with four, in one derived `net-1` group or four
labelled groups alike.

## Parallel pages

```yaml
pages:
  template: "https://news.ycombinator.com/news?p={n}"
  from: 1
  to: 10
concurrency: 4
```

Or an explicit list:

```yaml
urls:
  - "https://example.com/a"
  - "https://example.com/b"
```

Rows are emitted in page order regardless of which response arrived first.
A `pages:` range stops after any two consecutive `not_found` or `empty` pages,
not only at the end of the range: a gap in the middle ends the run early. Those
pages are not incomplete, so the run can still exit 0.

## More commands

```bash
cscrape try <url> "p.price_color" --in "article.product_pod"   # test a field
cscrape fetch <url> --out page.html                             # save once, iterate offline
cscrape probe <url>                                             # tier verdict
cscrape verify data.csv --min-rows 10                           # check a dataset after the fact
```

`try` also flags pages rendered by JavaScript, points at embedded JSON, and
warns about brittle DevTools "Copy selector" paths. [docs/selectors.md](docs/selectors.md)
is the full guide.

## What it will not do

- Log in, get past login walls, or use credentials that are not yours.
- Promise that a site works today because it worked on another day.
- Beat fingerprint or JavaScript challenges (Cloudflare managed challenge,
  PerimeterX, DataDome) with proxies. Those pages report `blocked`; a proxy you
  bring gets past IP bans, IP-range bans and rate limits, not a challenge.
- Ship scraped data. It ships the tool.

## Why trust the numbers

Every run records one outcome per page, and the daily live check scrapes the
same example sites asserted at exit 0, so a quiet regression shows up as a red
check rather than a friendly empty CSV.

## The old name

The `scrapeforge` command still works until 0.5.0 and prints one deprecation
notice on stderr; `import scrapeforge` also works and raises a
`DeprecationWarning`, which Python shows when the import runs in `__main__`
(the usual script case) and hides inside imported modules. New code should use
`certainlyscraped` or `cscrape`; the old name is removed in 0.5.0.

## Development

```bash
git clone https://github.com/vhsgreed/certainlyscraped && cd certainlyscraped
pip install -e ".[dev]"
python -m pytest -q
```

Tests run offline against saved pages in `tests/fixtures/`. CI runs them on
Python 3.11 to 3.14. The live check runs the examples against real sites daily.
MIT licensed. Part of the [vhsgreed](https://vhsgreed.win) toolset.
