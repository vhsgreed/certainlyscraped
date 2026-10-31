"""Pagination loop: follow next-page links/selectors up to max_pages.

Two entry points over one loop: :func:`follow_links` returns a
``PageOutcome`` per page (what the API and CLI report on), and
:func:`paginate` keeps the older ``(get_html, on_page(html)) -> int`` shape
for callers that only need a page count.
"""
from __future__ import annotations

import time
from urllib.parse import urljoin

from .outcome import PageFetch, PageOutcome, classify_fetch


def follow_links(fetch_page, base_url: str, cfg: dict, on_page,
                 log=None) -> list[PageOutcome]:
    """Fetch every page, recording one ``PageOutcome`` per page.

    fetch_page(url) -> PageFetch; on_page(html, url) -> rows added by the page
    (0 marks an otherwise-good page ``empty``, not ``ok``).
    log: stream for a line when an error status ends pagination early
         (a block or rate limit otherwise looks like the last page).
    Returns the outcomes in fetch order.
    """
    pag = cfg.get("paginate") or {}
    max_pages = min(cfg.get("max_pages", 10), 100)
    rate = cfg.get("rate_limit_sec", 2.0)

    url = base_url
    seen = {url}
    pages = 0          # pages successfully fetched; error pages never count
    outcomes: list[PageOutcome] = []
    from bs4 import BeautifulSoup

    while url and pages < max_pages:
        fetched = fetch_page(url)
        verdict = classify_fetch(url, fetched.status, fetched.html)
        outcome = PageOutcome(
            url=url, outcome=verdict.outcome, status=fetched.status,
            attempts=fetched.attempts, steps=list(fetched.steps),
            proxy_group=fetched.proxy_group, elapsed_ms=fetched.elapsed_ms,
            note=verdict.note)
        if fetched.status >= 400:
            # Recorded, then stop: the rest of the pagination is unreachable.
            outcomes.append(outcome)
            if log:
                print(f"[paginate] stopped: HTTP {fetched.status} on {url}",
                      file=log)
            break
        pages += 1
        rows = on_page(fetched.html, url) or 0
        outcome.rows = rows
        if outcome.outcome == "ok" and rows == 0:
            outcome.outcome = "empty"
        outcomes.append(outcome)

        if not pag:
            break
        soup = BeautifulSoup(fetched.html, "html.parser")
        sel = pag.get("selector")
        attr = pag.get("attribute", "href")
        next_el = None
        if pag.get("next_text"):
            for el in soup.select(sel or "a"):
                if pag["next_text"].lower() in el.get_text(" ", strip=True).lower():
                    next_el = el
                    break
        else:
            next_el = soup.select_one(sel) if sel else None
        if next_el is None:
            break
        href = next_el.get(attr)
        if not href or href.startswith("javascript:"):
            break
        # Resolve against the page the link came from, not the start URL:
        # a relative "?page=3" on /cat/page/2/ must stay under /cat/page/2/.
        url = urljoin(url, href)
        if url in seen:  # next link points back at a page already fetched
            break
        seen.add(url)
        if rate:
            time.sleep(rate)
    return outcomes


def paginate(get_html, base_url: str, cfg: dict, on_page, log=None) -> int:
    """Calls get_html(url) -> (status, html), on_page(html) per page.

    Kept for API compatibility on top of :func:`follow_links`; the return is
    the number of pages that came back with a status below 400.
    """
    def fetch_page(url):
        status, html = get_html(url)
        return PageFetch(status=status, html=html)

    def handle_page(html, url):
        on_page(html)
        return 0

    outcomes = follow_links(fetch_page, base_url, cfg, handle_page, log=log)
    return sum(1 for o in outcomes if o.status < 400)
