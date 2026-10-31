"""Ordered parallel runner and page sources (no network).

Run: python3 -m pytest tests/ -q
"""
import os
import random
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, ".")

import pytest

from certainlyscraped import scrape
from certainlyscraped.outcome import PageFetch
from certainlyscraped.runner import RateGate, page_source, run_parallel

FAST = {"rate_limit_sec": 0}


class FakeClock:
    """Monotonic clock whose sleep advances time instead of blocking."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def _html_fetcher(pages: dict, delay=0.0, fetched=None):
    def fetch_page(url):
        if fetched is not None:
            fetched.append(url)
        if delay:
            time.sleep(random.uniform(0, delay))
        status, html = pages.get(url, (404, ""))
        return PageFetch(status=status, html=html)
    return fetch_page


# --------------------------------------------------------------------------
# 1. rows keep URL order no matter which fetch finishes first
# --------------------------------------------------------------------------

def test_rows_in_page_order_under_random_latency():
    urls = [f"https://s/p{i}" for i in range(1, 9)]
    pages = {u: (200, u) for u in urls}
    runs = []
    for _ in range(3):
        seen = []

        def on_page(html, url):
            seen.append(url)
            return 1

        outcomes = run_parallel(_html_fetcher(pages, delay=0.05), urls,
                                dict(FAST, concurrency=4), on_page)
        assert [o.url for o in outcomes] == urls
        runs.append(list(seen))
    assert runs[0] == urls
    assert runs[0] == runs[1] == runs[2]


# --------------------------------------------------------------------------
# 2. rate gate spaces starts per (host, proxy group)
# --------------------------------------------------------------------------

def test_rate_gate_per_host():
    fc = FakeClock()
    gate = RateGate(1.0, clock=fc.clock, sleep=fc.sleep)
    times = []
    for _ in range(3):
        gate.wait(("h", None))
        times.append(fc.clock())
    assert times == [0, 1, 2]

    fc2 = FakeClock()
    gate2 = RateGate(1.0, clock=fc2.clock, sleep=fc2.sleep)
    gate2.wait(("h", "a"))
    assert fc2.clock() == 0
    gate2.wait(("h", "b"))
    assert fc2.clock() == 0
    assert fc2.sleeps == []


def test_run_parallel_paces_fetch_starts():
    """The gate is wired into the run: 4 starts, one per 0.1 s interval."""
    urls = [f"https://shop.test/p{i}" for i in range(1, 5)]
    pages = {u: (200, u) for u in urls}
    starts = []

    def fetch_page(url):
        starts.append(time.monotonic())
        return PageFetch(status=200, html=url)

    outcomes = run_parallel(fetch_page, urls,
                            {"rate_limit_sec": 0.1, "concurrency": 4},
                            lambda html, url: 1)

    assert [o.url for o in outcomes] == urls
    assert len(starts) == 4
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(gap >= 0.09 for gap in gaps), gaps
    assert starts[-1] - starts[0] >= 0.28, starts


# Ctrl-C has to leave the process promptly even while the gate is pacing: the
# gate must be waited on by the calling thread, not by non-daemon pool workers
# (which the interpreter joins at exit). A real subprocess and a real SIGINT
# are the only way to catch the exit delay; in-process tests cannot.
CTRL_C_SCRIPT = """
import sys
from certainlyscraped.outcome import PageFetch
from certainlyscraped.runner import run_parallel


def fetch_page(url):
    return PageFetch(status=200, html="<html><body>ok</body></html>")


def on_page(html, url):
    return 1


urls = ["https://shop.test/news?p=%d" % n for n in range(1, 13)]
print("READY", flush=True)
run_parallel(fetch_page, urls, {"concurrency": 4, "rate_limit_sec": 1.0},
             on_page)
print("FINISHED", flush=True)
"""


def test_ctrl_c_exits_promptly_while_gate_waits():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen([sys.executable, "-c", CTRL_C_SCRIPT], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        assert proc.stdout.readline().strip() == "READY"
        time.sleep(0.3)  # now inside the calling thread's gate wait
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AssertionError("process ignored SIGINT for over 10 s")
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode != 0, "SIGINT should stop the run, not finish it"
    assert elapsed < 1.5, f"exit took {elapsed:.2f}s after SIGINT (want <1.5s)"


def test_rate_gate_disabled_intervals_do_not_sleep():
    fc = FakeClock()
    RateGate(0, clock=fc.clock, sleep=fc.sleep).wait(("h", None))
    RateGate(-1, clock=fc.clock, sleep=fc.sleep).wait(("h", None))
    assert fc.sleeps == []


# --------------------------------------------------------------------------
# 3./4. trailing-stop only for ranges
# --------------------------------------------------------------------------

def test_range_stops_after_two_trailing_not_found():
    urls = [f"https://s/news?p={n}" for n in range(1, 11)]
    pages = {u: (200, u) for u in urls[:3]}  # 4..10 are 404
    fetched = []
    added = []

    outcomes = run_parallel(_html_fetcher(pages, fetched=fetched), urls,
                            dict(FAST, concurrency=1),
                            lambda html, url: added.append(url) or 1,
                            stop_on_trailing=True)

    assert [o.outcome for o in outcomes] == \
        ["ok", "ok", "ok", "not_found", "not_found"]
    assert [o.url for o in outcomes] == urls[:5]
    # Page 6+ was never fetched (or, at worst, its result was dropped).
    assert all("p=6" not in u and "p=7" not in u and "p=8" not in u
               and "p=9" not in u and "p=10" not in u for u in fetched)
    assert added == urls[:3]


def test_list_does_not_stop_on_not_found():
    urls = [f"https://s/a{i}.html" for i in range(5)]
    pages = {u: (200, u) for u in urls}
    pages[urls[2]] = (404, "")
    fetched = []

    outcomes = run_parallel(_html_fetcher(pages, fetched=fetched), urls,
                            dict(FAST, concurrency=2), lambda h, u: 1,
                            stop_on_trailing=False)

    assert [o.url for o in outcomes] == urls
    assert [o.outcome for o in outcomes] == \
        ["ok", "ok", "not_found", "ok", "ok"]
    assert sorted(fetched) == sorted(urls)


# --------------------------------------------------------------------------
# 5. config errors and range construction
# --------------------------------------------------------------------------

def test_page_source_links_by_default():
    assert page_source({"url": "https://s/start"}) == \
        ("links", ["https://s/start"])


def test_page_source_expands_range():
    kind, urls = page_source({"pages": {
        "template": "https://s/news?p={n}", "from": 1, "to": 3}})
    assert kind == "range"
    assert urls == ["https://s/news?p=1", "https://s/news?p=2",
                    "https://s/news?p=3"]


def test_page_source_list_is_verbatim():
    urls = ["https://s/a", "https://s/b"]
    assert page_source({"urls": urls}) == ("list", urls)


def test_two_sources_is_config_error():
    with pytest.raises(ValueError) as err:
        page_source({"url": "https://s/", "paginate": {"selector": "a"},
                     "pages": {"template": "https://s/?p={n}", "to": 2}})
    assert "paginate" in str(err.value) and "pages" in str(err.value)


def test_pages_and_urls_together_is_config_error():
    with pytest.raises(ValueError) as err:
        page_source({"urls": ["https://s/a"],
                     "pages": {"template": "https://s/?p={n}", "to": 2}})
    assert "pages" in str(err.value) and "urls" in str(err.value)


def test_template_without_n_is_config_error():
    with pytest.raises(ValueError) as err:
        page_source({"pages": {"template": "https://s/news", "to": 2}})
    assert "{n}" in str(err.value)


def test_from_greater_than_to_is_config_error():
    with pytest.raises(ValueError):
        page_source({"pages": {"template": "https://s/?p={n}",
                               "from": 5, "to": 2}})


def test_range_longer_than_100_is_clamped():
    kind, urls = page_source({"pages": {"template": "https://s/?p={n}",
                                        "from": 1, "to": 500}})
    assert kind == "range"
    assert len(urls) == 100
    assert urls[0] == "https://s/?p=1" and urls[-1] == "https://s/?p=100"


# --------------------------------------------------------------------------
# 6./7. failure handling
# --------------------------------------------------------------------------

def test_keyboard_interrupt_cancels_pending():
    urls = [f"https://s/p{i}" for i in range(1, 21)]
    started = []

    def fetch_page(url):
        started.append(url)
        if url.endswith("p2"):
            raise KeyboardInterrupt
        time.sleep(0.01)
        return PageFetch(status=200, html=url)

    before = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_parallel(fetch_page, urls, dict(FAST, concurrency=4),
                     lambda h, u: 1)
    elapsed = time.monotonic() - before
    assert elapsed < 1.0
    assert len(started) < 20


def test_single_page_error_does_not_kill_the_run():
    urls = [f"https://s/p{i}" for i in range(1, 5)]
    import io

    class FetchError(Exception):
        pass

    def fetch_page(url):
        if url.endswith("p2"):
            raise FetchError("boom")
        return PageFetch(status=200, html=url)

    log = io.StringIO()
    outcomes = run_parallel(fetch_page, urls, dict(FAST, concurrency=2),
                            lambda h, u: 1, log=log)

    assert [o.outcome for o in outcomes] == ["ok", "error", "ok", "ok"]
    assert outcomes[1].status == 0
    assert "FetchError" in outcomes[1].note and "boom" in outcomes[1].note
    assert "[parallel] https://s/p2: FetchError: boom" in log.getvalue()


def test_concurrency_actually_fetches_pages_in_parallel():
    """Mutation 20: `concurrency` is not ignored (one worker would peak at 1)."""
    lock = threading.Lock()
    state = {"in_flight": 0, "peak": 0}

    def fetch_page(url):
        with lock:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        time.sleep(0.05)
        with lock:
            state["in_flight"] -= 1
        return PageFetch(status=200, html=url)

    urls = [f"https://s/p{i}" for i in range(6)]
    outcomes = run_parallel(fetch_page, urls,
                            {"rate_limit_sec": 0, "concurrency": 3},
                            lambda html, url: 1)
    assert [o.url for o in outcomes] == urls
    assert state["peak"] >= 2, state


def test_zero_rows_marks_page_empty():
    urls = ["https://s/a", "https://s/b"]
    pages = {u: (200, u) for u in urls}
    outcomes = run_parallel(_html_fetcher(pages), urls,
                            dict(FAST, concurrency=2), lambda h, u: 0)
    assert [o.outcome for o in outcomes] == ["empty", "empty"]


# --------------------------------------------------------------------------
# 8. api.scrape integration
# --------------------------------------------------------------------------

def _page(title):
    return (f'<html><body><div class="p"><a href="/{title.lower()}">'
            f'{title}</a></div></body></html>')


ITEM_CFG = {
    "name": "t", "tier": 1, "rate_limit_sec": 0, "concurrency": 2,
    "items": {"selector": "div.p"},
    "fields": {"title": {"selector": "a"}},
}


def _title_fetcher(*statuses):
    def fetch(url):
        n = url.rsplit("=", 1)[-1].rsplit("/", 1)[-1].split(".")[0]
        try:
            status = statuses[int(n) - 1]
        except (IndexError, ValueError):
            return 404, ""
        return status, _page(f"T{n}") if status == 200 else ""
    return fetch


def test_scrape_pages_range_uses_all_pages():
    cfg = dict(ITEM_CFG, url="https://shop.test/news?p=1",
               pages={"template": "https://shop.test/news?p={n}",
                      "from": 1, "to": 4})
    res = scrape(cfg, fetcher=_title_fetcher(200, 200, 200, 200), log=None)
    assert [r["title"] for r in res.rows] == ["T1", "T2", "T3", "T4"]
    assert [o.url for o in res.page_report] == \
        [f"https://shop.test/news?p={n}" for n in range(1, 5)]
    assert res.pages == 4


def test_scrape_urls_list_keeps_config_order():
    urls = ["https://shop.test/a/1.html", "https://shop.test/a/2.html"]
    cfg = dict(ITEM_CFG, urls=urls)
    res = scrape(cfg, fetcher=_title_fetcher(200, 200), log=None)
    assert [r["title"] for r in res.rows] == ["T1", "T2"]
    assert [o.url for o in res.page_report] == urls
    assert res.pages == 2
