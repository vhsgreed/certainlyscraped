# Changelog

All notable changes to this project are documented here. Version 0.4.0 is the
first release under the name `certainlyscraped`; earlier releases shipped as
`scrapeforge`.

## [0.4.0] - 2026-10-31

### Release notes

- Rename the repository to `vhsgreed/certainlyscraped` before pushing the
  `0.4.0` tag: the README, badge and schema `$id` URLs already use the new
  name and 404 until the rename lands.

### Breaking

- The package, import name and primary command are now `certainlyscraped`.
  The short alias `cscrape` is the intended command in scripts.

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
  a retry do not count.
- **MCP server.** `certainlyscraped mcp` serves `probe`, `init`,
  `try_selector` and `scrape` over stdio (install with
  `pip install "certainlyscraped[mcp]"`). Every result is a status dict, never
  an exception. Private and loopback addresses are refused by default
  (`--allow-private` for local development), and proxies come only from the
  server's own `--proxies` file.
- **Parallel page sources.** `pages:` expands a `{n}` URL template over a
  range and `urls:` takes an explicit list; `concurrency` fetches them with a
  bounded thread pool. Rows are emitted in page order regardless of completion
  order, and a range stops after two trailing empty or missing pages.
- **Escalation ladder.** Pages start at the cheapest tier that works. A rate
  limit tries another proxy group or waits once; a challenge raises the tier;
  a plain 403 tries another group before raising the tier. The level is sticky
  per site, and `max_tier`, `max_retries`, `cooldown_sec` and
  `per_proxy_rate_sec` tune it.
- **Proxy pool.** `proxies:` accepts groups, a proxies file, and a generic
  provider gateway whose password comes from `password_env`. A proxy that is
  walled for one host cools down for that host only. `--proxies`,
  `--no-proxy` and `CERTAINLYSCRAPED_PROXIES` set precedence.
- **Redaction everywhere.** Proxy URLs, hosts and passwords never appear in a
  page outcome or an MCP response; only the group label is shown. A log line
  for a failing proxy carries the redacted proxy URL (credentials masked),
  because that address is the diagnosis.

### Changed

- Tier 3 is still optional, heavy and never promised for a site. Through the
  MCP server the ladder stops at tier 2, because the private-address guard
  cannot police every hop of a browser.
- A password inside `proxy:` or `proxies:` in the main config is refused with
  a message pointing at a proxies file or `password_env`.

### Deprecated

- The `scrapeforge` command still works in 0.4.x and prints a rename notice on
  stderr; `import scrapeforge` also works and raises a `DeprecationWarning`
  (hidden by default). Both are removed in 0.5.0.
