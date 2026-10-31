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
import time
from dataclasses import dataclass, field
from typing import Callable

import yaml

from .extract import extract_page
from .fetch import fetch as _fetch
from .outcome import PageFetch, PageOutcome, scrape_status, summary_line
from .paginate import follow_links

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


def scrape(config: str | dict, *, tier: int | None = None,
           fetcher: Fetcher | None = None, log=sys.stderr,
           guard=None) -> ScrapeResult:
    """Fetch every page the config paginates over and extract its items.

    tier: override the config's tier (skips the probe).
    fetcher: callable url -> (status, html); replaces the tiered fetcher,
             which is how tests and custom transports plug in.
    log: stream for progress lines, or None for silence.
    guard: callable(url) -> None (e.g. netguard.check_url) applied by the
           built-in fetcher to the probe and to every fetch hop. It is NOT
           applied to a caller-supplied `fetcher`; that fetcher owns its own
           network policy.
    """
    cfg = load_config(config)
    if tier is None:
        tier = 1 if fetcher else resolve_tier(cfg, log, guard=guard)
    if fetcher is None:
        def fetcher(u):
            return _fetch(u, tier, timeout=cfg.get("timeout", 30),
                          proxy=cfg.get("proxy"), cookies=cfg.get("cookies"),
                          guard=guard)

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

    outcomes = follow_links(as_page_fetcher(fetcher, tier), cfg["url"], cfg,
                            on_page, log=log)
    pages = sum(1 for o in outcomes if o.status < 400)
    if log:
        print(summary_line(outcomes), file=log)
        print(f"[rows] {len(rows)} unique items", file=log)
    return ScrapeResult(rows, pages, tier, cfg, outcomes)
