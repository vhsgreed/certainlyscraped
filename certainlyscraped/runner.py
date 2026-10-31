"""Page sources: a URL list from config, fetched in parallel but consumed in order.

`page_source` turns the config into one of three shapes: ``links`` (today's
next-link pagination), a ``range`` expanded from a ``https://...{n}`` template,
or an explicit ``list`` of URLs. `run_parallel` fetches range/list sources with
a bounded thread pool while keeping everything the caller observes -- the
``on_page`` callback and the returned outcomes -- in URL order, so rows never
depend on which response arrived first.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.parse import urlparse

from .outcome import (PageFetch, PageOutcome, Verdict, classify_fetch,
                      outcome_for)

# Same ceiling as follow_links' max_pages: a template range is still a pager.
RANGE_CAP = 100
MAX_WORKERS = 16
# Consecutive trailing not_found/empty pages that end a range crawl.
TRAILING_STOP = 2

_SOURCES = ("paginate", "pages", "urls")
_TRAILING = frozenset({"not_found", "empty"})


def page_source(cfg: dict, log=None) -> tuple[str, list[str]]:
    """Resolve which page set to fetch: ('links'|'range'|'list', urls).

    Exactly one of ``paginate``/``pages``/``urls`` may be set; naming more is
    ambiguous, so it is a config error. ``pages`` is the only key that expands
    into a computed range, and ``log`` receives one line when that range is
    capped at :data:`RANGE_CAP` pages.
    """
    present = [key for key in _SOURCES if cfg.get(key) is not None]
    if len(present) > 1:
        raise ValueError(
            "config sets more than one page source ("
            + ", ".join(present)
            + "): use exactly one of paginate, pages, urls")
    if "pages" in present:
        return "range", _expand_range(cfg["pages"], log)
    if "urls" in present:
        return "list", _url_list(cfg["urls"])
    url = cfg.get("url")
    if not url:
        raise ValueError(
            "config has no page source: set url, pages, or urls")
    return "links", [url]


def _expand_range(spec, log=None) -> list[str]:
    if not isinstance(spec, dict):
        raise ValueError("pages must be a mapping with template, from, to")
    template = spec.get("template")
    if not isinstance(template, str) or "{n}" not in template:
        raise ValueError("pages.template must be a string containing {n}")
    try:
        start = int(spec.get("from", 1))
        stop = int(spec.get("to", 1))
    except (TypeError, ValueError):
        raise ValueError("pages.from and pages.to must be whole numbers") \
            from None
    if stop < start:
        raise ValueError(
            f"pages.from ({start}) is greater than pages.to ({stop})")
    if stop - start + 1 > RANGE_CAP:
        capped = start + RANGE_CAP - 1
        if log:
            print(f"[pages] range {start}..{stop} capped at {RANGE_CAP} pages "
                  f"({start}..{capped})", file=log)
        stop = capped
    return [template.replace("{n}", str(n)) for n in range(start, stop + 1)]


def _url_list(urls) -> list[str]:
    if isinstance(urls, str) or not isinstance(urls, (list, tuple)):
        raise ValueError("urls must be a list of absolute URLs")
    if not urls:
        raise ValueError("urls is empty: list at least one URL")
    return [str(u) for u in urls]


class RateGate:
    """One fetch start per ``interval`` seconds per key, thread-safe.

    key is ``(host, proxy_group)`` so two proxies to the same host each get
    their own slot. A slot is reserved under the lock and slept for outside
    it, so threads never hold the gate while waiting.
    """

    def __init__(self, interval: float, clock=time.monotonic,
                 sleep=time.sleep) -> None:
        self.interval = float(interval or 0)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next: dict[tuple, float] = {}

    def wait(self, key: tuple) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = self._clock()
            last = self._next.get(key)
            slot = now if last is None or now >= last else last
            self._next[key] = slot + self.interval
            delay = slot - now
        if delay > 0:
            self._sleep(delay)


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def _clamp(value, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def run_parallel(fetch_page, urls: list[str], cfg: dict, on_page,
                 log=None, stop_on_trailing: bool = False) -> list[PageOutcome]:
    """Fetch ``urls`` concurrently; feed ``on_page`` in URL order.

    fetch_page(url) -> PageFetch. Results are consumed on the calling thread in
    URL order, so ``on_page`` never runs on a worker and rows are
    deterministic. A fetch that raises becomes an ``error`` outcome for that
    page only; KeyboardInterrupt cancels the rest and propagates.

    With ``stop_on_trailing`` (range sources only), two consecutive
    not_found/empty pages end the crawl and the remaining pages are dropped.
    """
    urls = list(urls)
    outcomes: list[PageOutcome] = []
    if not urls:
        return outcomes

    workers = _clamp(cfg.get("concurrency", 1), 1, MAX_WORKERS)
    gate = RateGate(cfg.get("rate_limit_sec", 1.0))
    executor = ThreadPoolExecutor(max_workers=workers,
                                  thread_name_prefix="cs-parallel")
    pending: dict[int, Future] = {}
    next_index = 0
    trailing = 0

    def fill() -> None:
        nonlocal next_index
        while next_index < len(urls) and len(pending) < workers:
            url = urls[next_index]
            # Take the slot on the calling thread, before submitting: a pool
            # worker that slept here would be joined as non-daemon at exit,
            # so Ctrl-C would hang for the rest of the gate interval.
            gate.wait((_host(url), None))
            pending[next_index] = executor.submit(fetch_page, url)
            next_index += 1

    def abort() -> None:
        for future in pending.values():
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    try:
        fill()
        index = 0
        while index < len(urls) and index in pending:
            url = urls[index]
            future = pending.pop(index)
            try:
                fetched = future.result()
            except Exception as exc:  # one bad page must not end the run
                detail = f"{type(exc).__name__}: {exc}"
                if log:
                    print(f"[parallel] {url}: {detail}", file=log)
                outcome = outcome_for(url, None, Verdict("error", detail))
            else:
                outcome = _consume(url, fetched, on_page)
            outcomes.append(outcome)
            trailing = trailing + 1 if outcome.outcome in _TRAILING else 0
            index += 1
            if stop_on_trailing and trailing >= TRAILING_STOP:
                break
            fill()
    except BaseException:
        abort()
        raise

    if pending:  # stopped early: drop the futures we never consumed
        abort()
    else:
        executor.shutdown(wait=True)
    return outcomes


def _consume(url: str, fetched: PageFetch, on_page) -> PageOutcome:
    verdict = classify_fetch(url, fetched.status, fetched.html)
    if fetched.status >= 400 or fetched.status == 0:
        return outcome_for(url, fetched, verdict)
    rows = on_page(fetched.html, url) or 0
    return outcome_for(url, fetched, verdict, rows=rows)
