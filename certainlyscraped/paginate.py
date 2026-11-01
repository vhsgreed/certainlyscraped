"""Pagination loop: follow next-page links/selectors up to max_pages.

Two entry points over one loop: :func:`follow_links` returns a
``PageOutcome`` per page (what the API and CLI report on), and
:func:`paginate` keeps the older ``(get_html, on_page(html)) -> int`` shape
for callers that only need a page count.
"""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

from .fetch import redact
from .outcome import PageFetch, PageOutcome, Verdict, fetch_verdict, outcome_for
from .runner import DEFAULT_GATE, RateGate

# A page that ended this way stops the walk: the next link is unreachable.
_STOP_PAGE = frozenset({"blocked", "rate_limited", "error", "not_found"})


def follow_links(fetch_page, base_url: str, cfg: dict, on_page,
                 log=None, gate=DEFAULT_GATE) -> list[PageOutcome]:
    """Fetch every page, recording one ``PageOutcome`` per page.

    fetch_page(url) -> PageFetch; on_page(html, url) -> rows added by the page
    (0 marks an otherwise-good page ``empty``, not ``ok``).
    log: stream for a line when an error status ends pagination early
         (a block or rate limit otherwise looks like the last page).
    gate: the rate gate; by default one is built from ``rate_limit_sec``.
          Pass ``None`` when the fetcher paces every attempt itself (the
          ladder), so a page is never waited for twice.
    Returns the outcomes in fetch order.
    """
    pag = cfg.get("paginate") or {}
    max_pages = min(cfg.get("max_pages", 10), 100)
    rate = cfg.get("rate_limit_sec", 2.0)
    if gate is DEFAULT_GATE:
        gate = RateGate(rate)

    url = base_url
    seen = {url}
    pages = 0          # pages successfully fetched; error pages never count
    outcomes: list[PageOutcome] = []
    from bs4 import BeautifulSoup

    while url and pages < max_pages:
        if gate is not None:
            gate.wait((urlparse(url).hostname or "", None))
        try:
            fetched = fetch_page(url)
        except Exception as exc:  # noqa: BLE001 - one page, not the whole run
            # R28: the links path records a bad page exactly as the parallel
            # runner does, and the message is scrubbed before it is logged or
            # put in an outcome (a client quotes its proxy URL verbatim).
            detail = redact(f"{type(exc).__name__}: {exc}")
            if log:
                print(f"[paginate] {url}: {detail}", file=log)
            outcomes.append(outcome_for(url, None, Verdict("error", detail)))
            break
        verdict = fetch_verdict(url, fetched)
        if fetched.status >= 400 or verdict.outcome in _STOP_PAGE:
            # Recorded, then stop: the rest of the pagination is unreachable.
            outcomes.append(outcome_for(url, fetched, verdict,
                                       note=fetched.note or None))
            if log:
                print(f"[paginate] stopped: HTTP {fetched.status} on {url}",
                      file=log)
            break
        pages += 1
        counted = on_page(fetched.html, url)   # None when the caller does not count
        rows = counted or 0
        outcomes.append(outcome_for(url, fetched, verdict, rows=rows,
                                    note=fetched.note or None))

        if not pag:
            break
        if pag.get("url"):
            # Some listings render no next-page anchor at all (Blocket,
            # Tradera): the list is addressable only by page number. Walk the
            # template instead of hunting for a link that is not there.
            if counted == 0:
                break                      # past the end of the list
            url = urljoin(base_url, pag["url"].replace("{page}", str(pages + 1)))
            if url in seen:
                break
            seen.add(url)
            continue
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
        # Pass the caller's row count through: the URL-template walk stops on
        # a page with no rows, and a callback that counts nothing is None.
        return on_page(html)

    outcomes = follow_links(fetch_page, base_url, cfg, handle_page, log=log)
    return sum(1 for o in outcomes if o.status < 400)
