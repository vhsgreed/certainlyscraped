"""Python API: the same pipeline `certainlyscraped run` uses, callable from code.

    from certainlyscraped import scrape
    result = scrape("examples/hn-front.yaml")
    for row in result.rows:
        ...
    if result.status() == "incomplete":
        ...

`config` is a path to a YAML file or an already-loaded dict with the same
keys. Nothing is written to disk; use `certainlyscraped.output.write` for that.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import yaml

from .escalate import Ladder
from .extract import extract_page
from .fetch import fetch as _fetch
from .fetch import redact as _redact
from .outcome import PageFetch, PageOutcome, scrape_status, summary_line
from .paginate import follow_links
from .proxypool import ProxyPool
from .runner import DEFAULT_GATE, page_source, run_parallel

Fetcher = Callable[[str], "tuple[int, str]"]


@dataclass
class ScrapeResult:
    rows: list[dict]
    pages: int
    tier: int
    config: dict = field(repr=False, default_factory=dict)
    page_report: list[PageOutcome] = field(default_factory=list)

    def status(self, min_rows: int = 1) -> str:
        """complete | incomplete | too_few_rows | blocked | error."""
        return scrape_status(len(self.rows), self.page_report, min_rows)


def load_config(config: str | dict) -> dict:
    if isinstance(config, dict):
        return config
    with open(config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"{config}: config must be a YAML mapping")
    return cfg


def resolve_tier(cfg: dict, log=None, guard=None) -> int:
    tier = cfg.get("tier", "auto")
    if tier != "auto":
        return int(tier)
    if str(cfg["url"]).startswith("file://"):
        return 1
    from .classify import probe
    res = probe(cfg["url"], guard=guard)
    if log:
        print(f"[probe] {cfg['url']} -> tier {res.tier} ({res.note})", file=log)
    return res.tier


def as_page_fetcher(fetcher: Callable[[str], tuple[int, str]],
                    tier: int) -> Callable[[str], PageFetch]:
    """Wrap a `url -> (status, html)` fetcher as a timing PageFetch source."""
    def fetch_page(url: str) -> PageFetch:
        start = time.monotonic()
        status, html = fetcher(url)
        return PageFetch(status=status, html=html, steps=[f"t{tier}"],
                         elapsed_ms=int((time.monotonic() - start) * 1000))
    return fetch_page


class _ScrubbingLog:
    """A log stream that redacts every write.

    The runner and the ladder print to whatever stream they are handed, so a
    wrapper here is the one place that covers every line they emit (including
    a library's own exception text) without each of them learning about
    proxies.
    """

    def __init__(self, stream, scrub) -> None:
        self._stream = stream
        self._scrub = scrub

    def write(self, text):
        return self._stream.write(self._scrub(text))

    def flush(self):
        flush = getattr(self._stream, "flush", None)
        if flush is not None:
            flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def scrape(config: str | dict, *, tier: int | None = None,
           fetcher: Fetcher | None = None, log=sys.stderr,
           guard=None) -> ScrapeResult:
    """Fetch every page the config paginates over and extract its items.

    tier: override the config's tier (skips the probe). The ladder starts
          every host here and climbs from it when a page is walled.
    fetcher: callable url -> (status, html); replaces the tiered fetcher,
             which is how tests and custom transports plug in.
    log: stream for progress lines, or None for silence.
    guard: callable(url) -> None (e.g. netguard.check_url) applied by the
           built-in fetcher to the probe and to every fetch hop. It is NOT
           applied to a caller-supplied `fetcher`; that fetcher owns its own
           network policy.
    """
    cfg = load_config(config)
    # A proxy config (proxy:/proxies:) becomes a pool for this run only; a
    # stateful pool must never outlive the scrape that built it.
    pool = ProxyPool.from_config(cfg) if fetcher is None else None

    # A pool knows its secrets and its own addresses; a pool that does not
    # (a test double) still gets the generic credential mask.
    scrub = getattr(pool, "scrub", None)
    pool_secrets = getattr(pool, "secrets", None)

    def clean(text: str) -> str:
        """Progress text with proxy credentials removed."""
        if not text:
            return text
        if scrub is not None:
            return scrub(text)
        if pool_secrets is not None:
            return _redact(text, pool_secrets())
        return _redact(text)

    if pool is not None and log is not None:
        # Every line the ladder and the runner emit, scrubbed at the sink.
        log = _ScrubbingLog(log, clean)

    kind, urls = page_source(cfg, log=log)
    if not cfg.get("url"):
        # pages/urls configs carry no base url; the first page is the base
        # for the tier probe and for logging.
        cfg = dict(cfg, url=urls[0])
    if tier is None:
        tier = 1 if fetcher else resolve_tier(cfg, log, guard=guard)
    ladder = None
    cancel = None
    if fetcher is None:
        # The built-in path escalates per site: the ladder owns the tier
        # choice for every page and the pacing gate (so the runner is told
        # gate=None and never charges a page a second wait). Both the ladder
        # and the runner share one cancel event so Ctrl-C reaches a worker
        # that is parked in a backoff.
        cancel = threading.Event()
        ladder = Ladder(cfg, tier, pool=pool, fetch_fn=_fetch, guard=guard,
                        log=log, cancel=cancel)
        fetch_page = ladder.fetch_page
    else:
        fetch_page = as_page_fetcher(fetcher, tier)

    rows: list[dict] = []
    seen: set[str] = set()

    def on_page(html, url):
        added = 0
        for item in extract_page(html, cfg, url):
            key = json.dumps(item, ensure_ascii=False, sort_keys=True,
                             default=str)
            if key not in seen:
                seen.add(key)
                rows.append(item)
                added += 1
        return added

    gate = None if ladder is not None else DEFAULT_GATE
    if kind == "links":
        outcomes = follow_links(fetch_page, cfg["url"], cfg, on_page, log=log,
                                gate=gate)
    else:
        outcomes = run_parallel(fetch_page, urls, cfg, on_page, log=log,
                                stop_on_trailing=(kind == "range"), gate=gate,
                                cancel=cancel)
    for outcome in outcomes:
        # An outcome's note is what a dataset or an agent reads: same rule
        # as the log line it came from.
        if outcome.note:
            outcome.note = clean(outcome.note)
    pages = sum(1 for o in outcomes if o.status < 400)
    if log:
        print(clean(summary_line(outcomes)), file=log)
        print(f"[rows] {len(rows)} unique items", file=log)
    return ScrapeResult(rows, pages, tier, cfg, outcomes)
