# Changelog

All notable changes to this project are documented here. Version 0.4.0 is the
first release under the name `certainlyscraped`; earlier releases shipped as
`scrapeforge`.

## [0.4.2] - 2026-11-01

### Fixed

- A `type: float` field read a comma decimal as grouping: AliExpress's
  `315,98kr` came out as 31598. Without a `decimal` hint, a float now
  guesses the mark: with both marks the last one is the decimal, a lone
  comma is the decimal unless exactly three digits follow it, and a lone dot
  stays a decimal. `type: int` and fields with `decimal: ","` are unchanged.
- `examples/aliexpress.yaml` returned 0 rows: AliExpress renamed its classes.
  Re-made with `certainlyscraped init`; the file says how to refresh it.

## [0.4.1] - 2026-10-31

### Fixed

- `import scrapeforge` and `python -m scrapeforge` failed with
  `ModuleNotFoundError: No module named 'mcp'` when the optional `[mcp]`
  extra was not installed: the rename shim imported every submodule,
  including the MCP server. It now skips a module whose optional dependency
  is missing; a missing module of our own still raises. 0.4.0 was correct
  with `certainlyscraped[mcp]` installed and for the `certainlyscraped` /
  `cscrape` commands.

## [0.4.0] - 2026-10-31

### Release notes

- Rename the repository to `vhsgreed/certainlyscraped` before pushing the
  `0.4.0` tag: the README, badge and schema `$id` URLs already use the new
  name and 404 until the rename lands.

### Breaking

- The package, import name and primary command are now `certainlyscraped`.
  The short alias `cscrape` is the intended command in scripts.
- The still-working `scrapeforge` command now behaves like the new one, so a
  0.3 script sees two differences: a run that got its rows but lost a page to
  a wall, a rate limit or an error exits 4 instead of 0 (`--accept-partial`
  restores 0), and a 403 or a challenge page causes one extra request at tier
  2 instead of ending the page at tier 1.

### Added

- **Per-page outcomes.** Every run records one outcome per page (`ok`, `empty`,
  `not_found`, `blocked`, `rate_limited`, `error`) with the HTTP status, rows
  extracted, attempts, the rungs tried, proxy group and elapsed time.
  `--report report.jsonl` writes those records, and the same data is available
  as `ScrapeResult.page_report`.
- **Exit code 4 (incomplete).** A run that got at least `--min-rows` but lost a
  page to a wall, a rate limit or an error now exits 4 instead of pretending
  it was complete. `--accept-partial` maps 4 to 0 for scripts that only check
  for zero. Trailing `not_found`/`empty` pages and pages that succeeded after
  a retry do not count. At the default `--min-rows 1` an all-error run has no
  rows, so it exits 2; exit 1 for a page-level error needs `--min-rows 0`.
- **MCP server.** `certainlyscraped mcp` serves `probe`, `init`,
  `try_selector` and `scrape` over stdio (install with
  `pip install "certainlyscraped[mcp]"`). Every result is a status dict, never
  an exception. Private and loopback addresses are refused by default and a
  refusal is terminal for the page (`--allow-private` for local development).
  Proxies come only from the server's own `--proxies` file, never from a tool
  argument and never from `CERTAINLYSCRAPED_PROXIES`; `probe`, `init` and
  `try_selector` lease a proxy from that pool per call. Without `--proxies`
  the server has no pool and fetches directly.
- **Parallel page sources.** `pages:` expands a `{n}` URL template over a
  range and `urls:` takes an explicit list; `concurrency` fetches them with a
  bounded thread pool. Rows are emitted in page order regardless of completion
  order, and a range stops after any two consecutive empty or missing pages,
  not only at the end of the range.
- **Escalation ladder.** Pages start at the cheapest tier that works. A rate
  limit tries another proxy group or waits once; a challenge raises the tier;
  a plain 403 tries another group before raising the tier; a 5xx retries once
  on the same lease, then climbs. With a pool, a page with no response at all
  tries one other group once, then retries and climbs. The level is sticky per
  site, except a level reached only by an error climb. `max_tier`,
  `max_retries`, `cooldown_sec` and `per_proxy_rate_sec` tune it. A rate-limit
  wait and a group switch each print a `[ladder]` line, so a slow run does not
  look hung.
- **Proxy pool.** `proxies:` accepts groups, a proxies file, and a generic
  provider gateway whose password comes from `password_env`. A proxy that is
  walled for one host cools down for that host only. Derived group labels are
  neutral (`net-1`, `net-2`, ... in order of first appearance, one per /16,
  /48 or hostname), so no address reaches `steps` or `proxy_group`. Pacing is
  per (host, proxy), so N proxies to one host give N slots. `--proxies`,
  `--no-proxy` and `CERTAINLYSCRAPED_PROXIES` set precedence, and with a pool
  `tier: auto` skips the probe and starts at tier 1 so nothing is fetched from
  the origin IP.
- **Redaction everywhere.** Proxy URLs, hosts and passwords never appear in a
  page outcome or an MCP response; only the group label is shown. A page that
  failed on a dead proxy keeps the transport error as its note
  (`ConnectionError: ... Failed to connect to *** over proxy *** ...`), and an
  unresolvable host reads `DNSError: ... Could not resolve host ...`; no log
  line quotes the address. A private address refused by the MCP guard is
  terminal: one attempt, no retry, no climb.

### Changed

- The ladder's rate gate defaults to 1.0 s (`per_proxy_rate_sec`, falling back
  to `rate_limit_sec`); 0.3's `paginate` slept 2.0 s by default. A config
  without either key now fetches about twice as fast. The pace is per (host,
  proxy), so a pool with N proxies gets N slots for one host.
- With a proxy pool, `tier: auto` no longer probes: a probe is a direct
  request. The ladder starts at tier 1 and climbs, so the first request
  already leaves through a proxy.
- Tier 3 is still optional, heavy and never promised for a site. Through the
  MCP server the ladder stops at tier 2, because the private-address guard
  cannot police every hop of a browser.
- A password inside `proxy:` or `proxies:` in the main config is refused with
  a message pointing at a proxies file or `password_env`.
- A page that ended without a response now carries the transport error as its
  note (for example `ConnectionError: ...` or `DNSError: ...`) instead of the
  generic `max_tier 2 reached`, so a dead proxy and a typo'd host are
  distinguishable. A private address refused by the MCP guard ends the page
  after one attempt.

### Deprecated

- The `scrapeforge` command still works in 0.4.x and prints one rename notice
  on stderr; `import scrapeforge` also works and raises a `DeprecationWarning`
  (shown for `__main__`, hidden inside imported modules). Both are removed in
  0.5.0.
