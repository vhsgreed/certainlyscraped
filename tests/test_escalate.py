"""Escalation ladder (spec section 4). No network: every fetch is scripted.

Run: python3 -m pytest tests/ -q
"""
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, ".")

import pytest

import certainlyscraped.api as api
import certainlyscraped.escalate as escalate
from certainlyscraped.escalate import Ladder
from certainlyscraped.fetch import FetchError
from certainlyscraped.fetch import fetch as real_fetch
from certainlyscraped.outcome import PageFetch, classify_fetch
from certainlyscraped.runner import RateGate

OK = "<html><body>" + ("nice real content " * 200) + "</body></html>"
CHALLENGE = ("<html><title>Just a moment...</title>"
             "<body>cf-chl challenge</body></html>")
FORBIDDEN = "<html><body>go away</body></html>"


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


def scripted(*responses):
    """fetch_fn returning the next scripted (status, html[, headers]) each call."""
    calls = []

    def fetch_fn(url, tier, **kwargs):
        calls.append({"url": url, "tier": tier, **kwargs})
        index = min(len(calls) - 1, len(responses) - 1)
        return responses[index]

    fetch_fn.calls = calls
    return fetch_fn


def make(responses, start=1, cfg=None, **kwargs):
    clock = kwargs.pop("clock", None) or FakeClock()
    conf = {"max_retries": 3, "rate_limit_sec": 0, "cooldown_sec": 300}
    conf.update(cfg or {})
    fetch_fn = scripted(*responses)
    gate = kwargs.pop("gate", None)
    if gate is None:
        gate = RateGate(0, clock=clock.clock, sleep=clock.sleep)
    ladder = Ladder(conf, start, fetch_fn=fetch_fn, sleep=clock.sleep,
                    gate=gate, **kwargs)
    return ladder, fetch_fn, clock


# --- the rules ------------------------------------------------------------

def test_ok_first_try():
    ladder, fetch_fn, _ = make([(200, OK)])
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1"]
    assert page.attempts == 1
    assert page.status == 200
    assert classify_fetch("https://h.test/1", page.status, page.html).outcome \
        == "ok"
    assert fetch_fn.calls[0]["tier"] == 1


def test_rate_limited_without_pool_backs_off_once():
    ladder, fetch_fn, clock = make([(429, ""), (200, OK)])
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1", "t1"]
    assert page.attempts == 2
    assert clock.sleeps == [300]
    assert page.status == 200


def test_retry_after_honoured():
    ladder, _, clock = make([(429, "", {"retry-after": "7"}), (200, OK)])
    ladder.fetch_page("https://h.test/1")
    assert clock.sleeps == [7]


def test_retry_after_capped_at_cooldown():
    ladder, _, clock = make([(429, "", {"retry-after": "86400"}), (200, OK)],
                            cfg={"cooldown_sec": 300})
    ladder.fetch_page("https://h.test/1")
    assert clock.sleeps == [300]


def test_challenge_escalates_tier():
    ladder, fetch_fn, _ = make([(403, CHALLENGE), (200, OK)])
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1", "t2"]
    assert [c["tier"] for c in fetch_fn.calls] == [1, 2]
    assert classify_fetch("https://h.test/1", page.status, page.html).outcome \
        == "ok"


def test_419_with_challenge_markers_climbs_instead_of_backing_off():
    """R36: a 419 with challenge markers is a challenge, so it climbs tier."""
    ladder, fetch_fn, clock = make([(419, CHALLENGE), (200, OK)])
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1", "t2"]
    assert [c["tier"] for c in fetch_fn.calls] == [1, 2]
    assert clock.sleeps == []  # a challenge is not a rate limit: no backoff
    assert classify_fetch("https://h.test/1", page.status, page.html).outcome \
        == "ok"


def test_419_without_markers_still_backs_off():
    ladder, fetch_fn, clock = make([(419, "Sorry"), (200, OK)])
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1", "t1"]
    assert clock.sleeps == [300]


def test_plain_403_without_pool_escalates():
    ladder, fetch_fn, _ = make([(403, FORBIDDEN), (200, OK)])
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1", "t2"]
    assert [c["tier"] for c in fetch_fn.calls] == [1, 2]


def test_sticky_level_per_host():
    pages = {
        "https://h.test/a": [(403, CHALLENGE), (200, OK)],
        "https://h.test/b": [(200, OK)],
        "https://other.test/c": [(200, OK)],
    }
    calls = []

    def fetch_fn(url, tier, **kwargs):
        calls.append((url, tier))
        seq = pages[url]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    grant = FakeClock()
    ladder = Ladder({"max_retries": 3, "rate_limit_sec": 0}, 1,
                    fetch_fn=fetch_fn, sleep=grant.sleep,
                    gate=RateGate(0, clock=grant.clock, sleep=grant.sleep))

    assert ladder.fetch_page("https://h.test/a").steps == ["t1", "t2"]
    assert ladder.level("h.test") == 2
    assert ladder.fetch_page("https://h.test/b").steps == ["t2"]
    # A different host starts fresh; the level did not leak.
    assert ladder.fetch_page("https://other.test/c").steps == ["t1"]
    assert ladder.level("other.test") == 1


@pytest.mark.parametrize("with_pool", [False, True])
def test_an_error_climb_is_not_remembered(with_pool):
    """R45: only a challenge/403 tier sticks, not one bought by no response."""
    # cooldown 0: the dead proxy is not parked, so page 2 shows the level alone
    pool = (pool_for(["http://10.0.0.1:8000 # a"], cooldown_sec=0)[0]
            if with_pool else None)
    suffix = "@a" if with_pool else ""

    def fetch_fn(url, tier, **kwargs):
        if url.endswith("/a"):
            raise FetchError("ConnectError: connection refused")
        return 200, OK

    ladder = _ladder_with(fetch_fn, cfg={"max_tier": 2}, pool=pool)
    first = ladder.fetch_page("https://h.test/a")
    # t1 error, same-tier retry, climb to t2 (remember = False), retry
    assert first.steps == [f"t1{suffix}", f"t1{suffix}",
                           f"t2{suffix}", f"t2{suffix}"]
    assert first.note == "ConnectError: connection refused"  # R42
    assert ladder.level("h.test") == 1

    second = ladder.fetch_page("https://h.test/b")
    assert second.steps == [f"t1{suffix}"]


def test_max_tier_caps_and_notes():
    ladder, _, _ = make([(403, CHALLENGE), (403, CHALLENGE)],
                        cfg={"max_tier": 2})
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1", "t2"]
    assert classify_fetch("https://h.test/1", page.status, page.html).outcome \
        == "blocked"
    assert "max_tier 2 reached" in page.note


def test_tier3_not_installed(monkeypatch):
    monkeypatch.setattr(escalate, "tier3_available", lambda: False)
    ladder, _, _ = make([(403, CHALLENGE), (403, CHALLENGE)],
                        cfg={"max_tier": 3})
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1", "t2"]
    assert "tier 3 not available" in page.note


def test_start_tier_three_falls_back_when_not_installed(monkeypatch):
    monkeypatch.setattr(escalate, "tier3_available", lambda: False)
    ladder, _, _ = make([(200, OK)], start=3, cfg={"max_tier": 3})
    assert ladder.fetch_page("https://h.test/1").steps == ["t2"]


def test_attempt_budget_is_max_retries_plus_one():
    """The budget still bounds the error path: retry, climb, retry, climb."""
    ladder, _, _ = make([(502, "bad gateway")], cfg={"max_retries": 3})
    page = ladder.fetch_page("https://h.test/1")
    assert page.attempts == 4
    assert page.steps == ["t1", "t1", "t2", "t2"]
    assert page.note == "max_tier 2 reached"


def test_configured_single_proxy_is_used_without_a_pool():
    ladder, fetch_fn, _ = make([(200, OK)],
                               cfg={"proxy": "http://single.example:3128"})
    ladder.fetch_page("https://h.test/1")
    assert fetch_fn.calls[0]["proxy"] == "http://single.example:3128"


def test_escalation_is_logged_once():
    import io
    log = io.StringIO()
    ladder, _, _ = make([(403, CHALLENGE), (200, OK)], log=log)
    ladder.fetch_page("https://h.test/1")
    assert ("[ladder] https://h.test/1: t1 -> t2 (challenge)"
            in log.getvalue())


def test_stop_note_reaches_the_page_outcome():
    from certainlyscraped.runner import run_parallel

    ladder, _, _ = make([(403, CHALLENGE), (403, CHALLENGE)],
                        cfg={"max_tier": 2})
    outcomes = run_parallel(ladder.fetch_page, ["https://h.test/1"],
                            {"rate_limit_sec": 0}, lambda html, url: 1,
                            gate=None)
    assert outcomes[0].outcome == "blocked"
    assert "max_tier 2 reached" in outcomes[0].note
    assert outcomes[0].steps == ["t1", "t2"]


# --- pacing and wiring ----------------------------------------------------

class FakeGate:
    def __init__(self):
        self.keys = []

    def wait(self, key):
        self.keys.append(key)


class _Proxy:
    def __init__(self, name):
        self.group = name
        self.url = f"http://{name}.example:8080"


class FakePool:
    def __init__(self, groups):
        self.groups = [_Proxy(g) for g in groups]
        self.reports = []

    def acquire(self, host):
        return self.groups[0] if self.groups else None

    def alternate(self, host, exclude_groups):
        for proxy in self.groups:
            if proxy.group not in exclude_groups:
                return proxy
        return None

    def report(self, proxy, host, outcome, *, challenge=False):
        self.reports.append((getattr(proxy, "group", None), host, outcome,
                             challenge))


def test_each_attempt_waits_on_the_gate_with_host_and_group():
    gate = FakeGate()
    pool = FakePool(["dc-a", "dc-b"])
    ladder, fetch_fn, _ = make([(200, OK)], gate=gate, start=2, pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert gate.keys == [("h.test", "dc-a")]
    assert page.steps == ["t2@dc-a"]
    assert page.proxy_group == "dc-a"
    assert fetch_fn.calls[0]["proxy"] == "http://dc-a.example:8080"


def test_pool_alternates_group_on_plain_403_before_escalating():
    pool = FakePool(["dc-a", "dc-b"])
    ladder, fetch_fn, _ = make([(403, FORBIDDEN), (200, OK)], pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1@dc-a", "t1@dc-b"]
    assert [c["tier"] for c in fetch_fn.calls] == [1, 1]
    assert page.proxy_group == "dc-b"


def test_api_scrape_uses_the_ladder_and_passes_no_gate(monkeypatch):
    captured = {}

    class StubLadder:
        def __init__(self, cfg, start_tier, **kwargs):
            captured["start_tier"] = start_tier

        def fetch_page(self, url):
            captured.setdefault("pages", []).append(url)
            return PageFetch(status=200, html=OK)

    monkeypatch.setattr(api, "Ladder", StubLadder)
    real = api.run_parallel

    def spy(fetch_page, urls, cfg, on_page, log=None,
            stop_on_trailing=False, gate="missing", cancel="missing"):
        captured["gate"] = gate
        return real(fetch_page, urls, cfg, on_page, log=log,
                    stop_on_trailing=stop_on_trailing, gate=gate,
                    cancel=cancel)

    monkeypatch.setattr(api, "run_parallel", spy)
    cfg = {"tier": 1, "rate_limit_sec": 0,
           "pages": {"template": "https://h.test/{n}", "from": 1, "to": 2},
           "items": {"selector": "div.p"}, "fields": {"t": {"selector": "a"}}}
    api.scrape(cfg, log=None)
    assert captured["start_tier"] == 1
    assert captured["gate"] is None
    assert captured["pages"] == ["https://h.test/1", "https://h.test/2"]


def test_follow_links_skips_its_own_pacing_when_a_gate_is_none(monkeypatch):
    """A ladder paces; follow_links must not build or wait on a second gate."""
    import time

    import certainlyscraped.paginate as paginate

    def boom(*args, **kwargs):
        raise AssertionError("follow_links paced a page the ladder owns")

    monkeypatch.setattr(paginate, "RateGate", boom)
    monkeypatch.setattr(time, "sleep", boom)

    def fetch_page(url):
        return PageFetch(status=200, html="<html>end</html>")

    outcomes = paginate.follow_links(
        fetch_page, "https://h.test/a",
        {"max_pages": 2, "rate_limit_sec": 5}, lambda h, u: 1, gate=None)
    assert [o.outcome for o in outcomes] == ["ok"]


# --- fetch(return_headers=...) -------------------------------------------

def test_fetch_return_headers_keeps_two_tuple_default():
    import httpx

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "7"}, text="slow")

    transport = httpx.MockTransport(handler)
    url = "https://h.test/1"
    status, html, headers = real_fetch(url, 1, transport=transport,
                                       return_headers=True)
    assert (status, html) == (429, "slow")
    assert headers["retry-after"] == "7"
    assert all(key == key.lower() for key in headers)
    assert real_fetch(url, 1, transport=transport) == (429, "slow")


def test_tier3_available_helper():
    import certainlyscraped.fetch as fetch_mod
    assert isinstance(fetch_mod.tier3_available(), bool)


# --- fix round 1: one backoff per page (R22) ------------------------------

def test_persistent_rate_limit_gets_exactly_one_backoff():
    """429 without Retry-After: back off once, retry once, then stop."""
    ladder, fetch_fn, clock = make([(429, "")], cfg={"max_retries": 3})
    page = ladder.fetch_page("https://h.test/1")
    assert clock.sleeps == [300]
    assert page.steps == ["t1", "t1"]
    assert page.attempts == 2
    assert classify_fetch("https://h.test/1", page.status, page.html).outcome \
        == "rate_limited"


def test_one_backoff_per_page_even_with_spare_attempts():
    """A budget of 4 must not buy 4 cooldowns."""
    ladder, _, clock = make([(429, "")], cfg={"max_retries": 10})
    page = ladder.fetch_page("https://h.test/1")
    assert clock.sleeps == [300]
    assert page.attempts == 2


def test_one_backoff_per_page_across_tiers():
    """429 at t1, challenge climb to t2, 429 again -> no second sleep."""
    ladder, _, clock = make([(429, ""), (403, CHALLENGE), (429, "")],
                            cfg={"max_retries": 3})
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1", "t1", "t2"]
    assert clock.sleeps == [300]
    assert sum(clock.sleeps) <= 300
    assert classify_fetch("https://h.test/1", page.status, page.html).outcome \
        == "rate_limited"


def test_no_sleep_after_the_last_attempt():
    """Budget already spent: the 429 ends the page without a pointless wait."""
    ladder, fetch_fn, clock = make([(429, "")], cfg={"max_retries": 0})
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1"]
    assert clock.sleeps == []
    assert len(fetch_fn.calls) == 1


def test_rate_limit_retry_honours_retry_after_once():
    ladder, _, clock = make([(429, "", {"retry-after": "7"})],
                            cfg={"max_retries": 3})
    ladder.fetch_page("https://h.test/1")
    assert clock.sleeps == [7]


# --- fix round 1: guard-aware tier 3 (R23) --------------------------------

def test_tier3_is_unusable_under_a_guard(monkeypatch):
    monkeypatch.setattr(escalate, "tier3_available", lambda: True)
    ladder, fetch_fn, _ = make([(403, CHALLENGE)],
                               cfg={"max_tier": 3, "max_retries": 3},
                               guard=lambda url: None)
    page = ladder.fetch_page("https://h.test/1")
    # Tier 3 is installed but the guard forbids it: never attempted.
    assert [c["tier"] for c in fetch_fn.calls] == [1, 2]
    assert page.steps == ["t1", "t2"]
    assert page.note == "tier 3 not available"
    assert classify_fetch("https://h.test/1", page.status, page.html).outcome \
        == "blocked"


def test_start_tier_three_falls_back_to_two_under_a_guard(monkeypatch):
    monkeypatch.setattr(escalate, "tier3_available", lambda: True)
    ladder, _, _ = make([(200, OK)], start=3, cfg={"max_tier": 3},
                        guard=lambda url: None)
    assert ladder.start_tier == 2
    assert ladder.fetch_page("https://h.test/1").steps == ["t2"]


def test_escalate_tier3_usable_needs_both(monkeypatch):
    ladder, _, _ = make([(200, OK)], cfg={"max_tier": 3})
    monkeypatch.setattr(escalate, "tier3_available", lambda: True)
    assert escalate.escalate_tier3_usable(ladder) is True
    ladder.guard = lambda url: None
    assert escalate.escalate_tier3_usable(ladder) is False
    monkeypatch.setattr(escalate, "tier3_available", lambda: False)
    ladder.guard = None
    assert escalate.escalate_tier3_usable(ladder) is False


# --- fix round 1: interruptible sleeps (R24) ------------------------------

def test_backoff_is_sliced_when_cancellable():
    cancel = threading.Event()
    clock = FakeClock()
    ladder, _, _ = make([(429, ""), (200, OK)], cancel=cancel, clock=clock)
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1", "t1"]
    assert sum(clock.sleeps) == 300
    assert max(clock.sleeps) <= 0.25
    assert len(clock.sleeps) > 1


def test_cancel_during_backoff_stops_the_page():
    cancel = threading.Event()
    clock = FakeClock()
    seen = []

    def sleep(seconds):
        seen.append(seconds)
        clock.t += seconds
        cancel.set()  # Ctrl-C lands mid-backoff

    conf = {"max_retries": 3, "rate_limit_sec": 0, "cooldown_sec": 300}
    ladder = Ladder(conf, 1, fetch_fn=scripted((429, "")), sleep=sleep,
                    gate=RateGate(0, clock=clock.clock, sleep=sleep),
                    cancel=cancel)
    started = time.monotonic()
    page = ladder.fetch_page("https://h.test/1")
    assert time.monotonic() - started < 1.0
    assert seen and max(seen) <= 0.25
    assert page.steps == ["t1"]  # no retry after the cancel
    assert classify_fetch("https://h.test/1", page.status, page.html).outcome \
        == "rate_limited"


def test_run_parallel_sets_cancel_when_interrupted():
    from certainlyscraped.runner import run_parallel

    cancel = threading.Event()

    def fetch_page(url):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_parallel(fetch_page, ["https://h.test/1"],
                     {"rate_limit_sec": 0, "concurrency": 1},
                     lambda html, url: 1, gate=None, cancel=cancel)
    assert cancel.is_set()


def test_api_scrape_shares_one_cancel_event(monkeypatch):
    captured = {}

    class StubLadder:
        def __init__(self, cfg, start_tier, **kwargs):
            captured["cancel"] = kwargs.get("cancel")

        def fetch_page(self, url):
            return PageFetch(status=200, html=OK)

    def spy(fetch_page, urls, cfg, on_page, log=None,
            stop_on_trailing=False, gate="missing", cancel="missing"):
        captured["runner_cancel"] = cancel
        return []

    monkeypatch.setattr(api, "Ladder", StubLadder)
    monkeypatch.setattr(api, "run_parallel", spy)
    cfg = {"tier": 1, "rate_limit_sec": 0,
           "pages": {"template": "https://h.test/{n}", "from": 1, "to": 2},
           "items": {"selector": "div.p"}, "fields": {"t": {"selector": "a"}}}
    api.scrape(cfg, log=None)
    assert isinstance(captured["cancel"], threading.Event)
    assert captured["cancel"] is captured["runner_cancel"]


# A real subprocess and a real SIGINT: a pool worker parked in a 300 s
# backoff is joined as a non-daemon thread at exit, so only an end-to-end
# interrupt proves the ladder's sleep is cancellable.
CTRL_C_BACKOFF_SCRIPT = """
import sys
from certainlyscraped import api

def fake_fetch(url, tier, **kwargs):
    return 429, "", {}

api._fetch = fake_fetch
cfg = {"tier": 1, "rate_limit_sec": 0, "cooldown_sec": 300,
       "max_retries": 3, "concurrency": 4,
       "pages": {"template": "https://h.test/{n}", "from": 1, "to": 10},
       "items": {"selector": "div.p"},
       "fields": {"t": {"selector": "a"}}}
print("READY", flush=True)
api.scrape(cfg, log=None)
print("FINISHED", flush=True)
"""


def test_ctrl_c_exits_promptly_during_a_backoff():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen([sys.executable, "-c", CTRL_C_BACKOFF_SCRIPT],
                            cwd=root, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "READY"
        time.sleep(0.4)  # workers are now inside a 300 s backoff
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AssertionError("process slept through SIGINT for over 10 s")
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode != 0, "SIGINT should stop the run, not finish it"
    assert elapsed < 1.5, f"exit took {elapsed:.2f}s after SIGINT (want <1.5s)"


# --- fix round 1: strict numeric config (R25) -----------------------------

@pytest.mark.parametrize("key,value", [
    ("max_tier", "abc"),
    ("max_tier", None),
    ("max_tier", float("inf")),
    ("max_tier", float("nan")),
    ("max_retries", "abc"),
    ("max_retries", None),
    ("max_retries", True),
    ("max_retries", float("inf")),
    ("cooldown_sec", "soon"),
    ("cooldown_sec", float("inf")),
])
def test_non_numeric_config_raises_value_error(key, value):
    with pytest.raises(ValueError) as exc:
        make([(200, OK)], cfg={key: value})
    assert key in str(exc.value)


def test_numeric_config_is_clamped_not_trusted():
    ladder, _, _ = make([(200, OK)], cfg={"max_tier": 9})
    assert ladder.max_tier == 3
    assert Ladder({"max_tier": 0}, 1, fetch_fn=scripted((200, OK)),
                  sleep=FakeClock().sleep, gate=FakeGate()).max_tier == 1
    assert Ladder({"max_retries": 9}, 1, fetch_fn=scripted((200, OK)),
                  sleep=FakeClock().sleep,
                  gate=FakeGate()).max_retries == 3
    assert Ladder({"max_retries": -1}, 1, fetch_fn=scripted((200, OK)),
                  sleep=FakeClock().sleep,
                  gate=FakeGate()).max_retries == 0
    assert Ladder({"cooldown_sec": -5}, 1, fetch_fn=scripted((200, OK)),
                  sleep=FakeClock().sleep,
                  gate=FakeGate()).cooldown_sec == 0.0


# --- proxy pool integration (Task 7) --------------------------------------

import json

import certainlyscraped.cli as cli
from certainlyscraped import fetch as fetch_mod
from certainlyscraped.proxypool import ProxyPool

PROXY_URL = "http://u:s3cret@10.1.2.3:8000"


def pool_for(entries, **kwargs):
    clock = FakeClock()
    pool = ProxyPool.from_config({"proxies": entries, **kwargs},
                                 env={}, clock=clock.clock)
    return pool, clock


def test_pool_switches_group_on_rate_limit():
    pool, _ = pool_for(["http://a:1 # a", "http://b:1 # b"])
    ladder, fetch_fn, _ = make([(429, ""), (200, OK)], pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1@a", "t1@b"]
    assert page.proxy_group == "b"
    assert [c["proxy"] for c in fetch_fn.calls] == ["http://a:1", "http://b:1"]


def test_pool_tries_other_group_before_climbing_tier():
    # R33: the page leases one proxy at a time. A plain 403 buys one other
    # group; with none left the tier climbs on the proxy the page already
    # holds (the parked one is not abandoned, and no fetch goes out bare).
    pool, _ = pool_for(["http://a:1 # a", "http://b:1 # b"])
    ladder, fetch_fn, _ = make(
        [(403, FORBIDDEN), (403, FORBIDDEN), (200, OK)], pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1@a", "t1@b", "t2@b"]
    assert [c["tier"] for c in fetch_fn.calls] == [1, 1, 2]
    assert [c["proxy"] for c in fetch_fn.calls] == [
        "http://a:1", "http://b:1", "http://b:1"]
    assert all(c["proxy"] is not None for c in fetch_fn.calls)
    assert page.proxy_group == "b"
    assert page.status == 200


def test_pool_cooldown_keeps_a_blocked_proxy_off_that_host():
    pool, clock = pool_for(["http://a:1 # a", "http://b:1 # b"])
    ladder, _, _ = make([(403, FORBIDDEN)], pool=pool)
    ladder.fetch_page("https://h.test/1")
    # Both groups reported blocked for the host: nothing is handed out.
    assert pool.acquire("h.test") is None
    # ... but another host still gets one.
    assert pool.acquire("other.test") is not None
    clock.t += 300
    assert pool.acquire("h.test") is not None


def test_a_page_whose_pool_is_cooling_never_fetches_from_the_origin(monkeypatch):
    """R27: a configured pool means never crawling without a proxy."""
    calls = []

    def fake_fetch(url, tier, **kwargs):
        calls.append((url, kwargs.get("proxy")))
        return 403, FORBIDDEN

    monkeypatch.setattr(api, "_fetch", fake_fetch)
    cfg = {"tier": 1, "max_tier": 1, "rate_limit_sec": 0, "concurrency": 1,
           "url": "https://h.test/1",
           "urls": ["https://h.test/1", "https://h.test/2"],
           "proxies": ["http://a:1 # a", "http://b:1 # b"],
           "items": {"selector": "li"},
           "fields": {"t": {"selector": "a"}}}
    result = api.scrape(cfg, log=None)

    # Page 1 tried both groups and parked both; page 2 never went out.
    assert [url for url, _ in calls] == ["https://h.test/1"] * 2
    assert all(proxy is not None for _, proxy in calls)
    first, second = result.page_report
    assert first.outcome == "blocked"
    assert second.outcome == "blocked"
    assert second.note == "no proxy available for h.test"
    assert second.status == 0 and second.attempts == 0


def test_a_pool_429_backs_off_once_then_retries_the_same_proxy():
    """R33 (b): the parked proxy keeps its lease: one 300 s wait, one retry.

    R27's version pinned a single call -- the ladder asked the pool again,
    got None and broke out, so a 429 paid the full backoff and gave up.
    """
    pool, _ = pool_for(["http://a:1 # a"])
    ladder, fetch_fn, clock = make([(429, "")], pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert len(fetch_fn.calls) == 2
    assert clock.sleeps == [300]
    assert [c["proxy"] for c in fetch_fn.calls] == ["http://a:1"] * 2
    assert page.steps == ["t1@a", "t1@a"]
    assert page.status == 429
    assert classify_fetch("https://h.test/1", page.status, page.html).outcome \
        == "rate_limited"
    # ... while the park still applies to the NEXT page on this host.
    assert pool.acquire("h.test") is None


def test_a_challenge_with_a_pool_climbs_on_the_same_proxy():
    """R33 (a): a challenge is about the tier, not the proxy."""
    pool, _ = pool_for(["http://a:1 # a"])
    ladder, fetch_fn, _ = make([(403, CHALLENGE), (200, OK)], pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1@a", "t2@a"]
    assert [c["tier"] for c in fetch_fn.calls] == [1, 2]
    assert [c["proxy"] for c in fetch_fn.calls] == ["http://a:1"] * 2
    assert page.status == 200


def test_a_page_whose_pool_is_cooling_for_the_host_stops_without_a_fetch():
    """R33 (c): no lease, no page -- and no request from the origin IP."""
    pool, _ = pool_for(["http://a:1 # a", "http://b:1 # b"])
    pool.report("http://a:1", "h.test", "blocked")
    pool.report("http://b:1", "h.test", "blocked")
    ladder, fetch_fn, _ = make([(200, OK)], pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert fetch_fn.calls == []
    assert page.attempts == 0 and page.steps == []
    assert page.outcome == "blocked"
    assert page.note == "no proxy available for h.test"


def test_a_park_from_one_page_steers_the_next_page_to_another_proxy():
    """R33 (d): the lease ends with the page; the park outlives it."""
    pool, _ = pool_for(["http://a:1 # a", "http://b:1 # b"])
    ladder, fetch_fn, _ = make(
        [(403, FORBIDDEN), (200, OK), (200, OK)], pool=pool)
    first = ladder.fetch_page("https://h.test/1")
    second = ladder.fetch_page("https://h.test/2")
    assert first.steps == ["t1@a", "t1@b"]
    assert first.proxy_group == "b"
    assert second.steps == ["t1@b"]
    assert second.proxy_group == "b"
    assert fetch_fn.calls[-1]["proxy"] == "http://b:1"


def test_a_challenge_does_not_park_the_proxy_for_the_next_page():
    """R34: the proxy worked (a challenge is the site); it stays available."""
    pool, _ = pool_for(["http://a:1 # a"])
    ladder, _, _ = make([(403, CHALLENGE), (200, OK), (200, OK)], pool=pool)
    first = ladder.fetch_page("https://h.test/1")
    second = ladder.fetch_page("https://h.test/2")
    assert first.steps == ["t1@a", "t2@a"]
    # sticky tier 2 for the host, and still proxy "a": nothing was parked
    assert second.steps == ["t2@a"]
    assert second.proxy_group == "a"


def test_no_secret_anywhere(tmp_path, monkeypatch):
    """Review Focus 1: a proxy failure must not leak its address anywhere."""
    import httpx

    def boom(url, tier, **kwargs):
        raise httpx.ProxyError(f"{PROXY_URL} refused")

    monkeypatch.setattr(api, "_fetch", boom)
    proxies = tmp_path / "proxies.txt"
    proxies.write_text(PROXY_URL + "\n", encoding="utf-8")
    cfg = {"tier": 1, "rate_limit_sec": 0, "concurrency": 1,
           "url": "https://h.test/1", "urls": ["https://h.test/1"],
           "proxies": {"list": str(proxies)},
           "items": {"selector": "li"},
           "fields": {"t": {"selector": "a"}}}
    import io
    log = io.StringIO()
    result = api.scrape(cfg, log=log)
    text = log.getvalue()
    assert "s3cret" not in text and "10.1.2.3" not in text
    assert result.page_report and result.page_report[0].outcome == "error"
    dumped = json.dumps([o.to_dict() for o in result.page_report])
    assert "s3cret" not in dumped and "10.1.2.3" not in dumped

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        "url: https://h.test/1\n"
        "urls: [https://h.test/1]\n"
        "tier: 1\n"
        "rate_limit_sec: 0\n"
        "concurrency: 1\n"
        "items: {selector: li}\n"
        "fields: {t: {selector: a}}\n", encoding="utf-8")
    report = tmp_path / "report.jsonl"
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", str(cfg_path), "--proxies", str(proxies),
                  "--out", str(tmp_path / "o.csv"),
                  "--report", str(report)])
    assert exc.value.code != 0
    body = report.read_text(encoding="utf-8")
    assert "s3cret" not in body and "10.1.2.3" not in body
    assert "***" in body


def test_fetch_accepts_a_proxy_object(monkeypatch):
    import httpx
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            return httpx.Response(200, text="ok")

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient(**kw))
    from certainlyscraped.proxypool import Proxy
    guarded = []
    status, _ = fetch_mod.fetch("https://h.test/1", 1,
                                proxy=Proxy("http://p:1", "g"),
                                guard=lambda url: guarded.append(url))
    assert status == 200
    assert seen["proxy"] == "http://p:1"
    assert guarded == ["http://p:1", "https://h.test/1"]


# --- fix round 3: transport failures carry their rung and proxy -----------

def test_a_transport_failure_keeps_its_rung_and_proxy(monkeypatch):
    """A ConnectError must reach the report as an error page that says
    which rung and which proxy it died on (README: 'exactly what it got
    and why it stopped')."""
    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)
    cfg = {"tier": 1, "max_tier": 1, "max_retries": 0, "rate_limit_sec": 0,
           "concurrency": 1, "url": "https://h.test/1",
           "urls": ["https://h.test/1"],
           "proxies": ["http://a:1 # a"],
           "items": {"selector": "li"},
           "fields": {"t": {"selector": "a"}}}
    result = api.scrape(cfg, log=None)
    page = result.page_report[0]
    assert page.outcome == "error"
    assert page.steps == ["t1@a"]
    assert page.proxy_group == "a"


def test_a_pooled_429_then_a_transport_failure_keeps_the_lease():
    from certainlyscraped.fetch import FetchError
    from certainlyscraped.runner import run_parallel

    pool, _ = pool_for(["http://a:1 # a"])
    clock = FakeClock()
    calls = []

    def fetch_fn(url, tier, **kwargs):
        calls.append((tier, kwargs.get("proxy")))
        if len(calls) == 1:
            return 429, "", {}
        raise FetchError("ConnectError: connection refused")

    ladder = Ladder({"max_retries": 1, "rate_limit_sec": 0, "cooldown_sec": 0},
                    1, pool=pool, fetch_fn=fetch_fn, sleep=clock.sleep,
                    gate=RateGate(0, clock=clock.clock, sleep=clock.sleep))
    outcomes = run_parallel(ladder.fetch_page, ["https://h.test/1"],
                            {"rate_limit_sec": 0}, lambda html, url: 1,
                            gate=None)
    page = outcomes[0]
    assert page.outcome == "error"
    assert page.steps == ["t1@a", "t1@a"]
    assert page.proxy_group == "a"
    assert [proxy for _, proxy in calls] == ["http://a:1", "http://a:1"]
    # The error was reported as network-level: the pair is parked.
    assert pool.acquire("h.test") is None


# --- fix round 3: the documented pool interface still works ---------------

class DocumentedPool:
    """A pool implementing only report(proxy, host, outcome)."""

    def __init__(self):
        self.reports = []

    def acquire(self, host):
        return _Proxy("a")

    def report(self, proxy, host, outcome):
        self.reports.append((proxy.group, host, outcome))


def test_a_pool_with_only_the_documented_report_signature_works():
    pool = DocumentedPool()
    ladder, _, _ = make([(200, OK)], pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert page.status == 200
    assert pool.reports == [("a", "h.test", "ok")]


# --- fix round A: I2 -- a transport failure keeps its reason --------------


def _ladder_with(fetch_fn, *, cfg=None, pool=None, clock=None, log=None):
    clock = clock or FakeClock()
    conf = {"max_retries": 3, "rate_limit_sec": 0, "cooldown_sec": 300}
    conf.update(cfg or {})
    return Ladder(conf, 1, pool=pool, fetch_fn=fetch_fn, sleep=clock.sleep,
                  log=log, gate=RateGate(0, clock=clock.clock,
                                         sleep=clock.sleep))


def test_final_transport_error_note_is_the_error_text():
    """I2/R42: a page that ends without a response says why."""
    def fetch_fn(url, tier, **kwargs):
        raise FetchError("ProxyError: all connection attempts failed")

    ladder = _ladder_with(fetch_fn, cfg={"max_retries": 1, "max_tier": 2})
    page = ladder.fetch_page("https://h.test/1")
    assert page.status == 0
    assert page.note == "ProxyError: all connection attempts failed"
    assert classify_fetch("https://h.test/1", page.status,
                         page.html).outcome == "error"


def test_transport_failures_are_distinguishable():
    """I2: a dead proxy and a DNS failure are not both 'max_tier 2 reached'."""
    def page_for(message):
        def fetch_fn(url, tier, **kwargs):
            raise FetchError(message)
        return _ladder_with(fetch_fn).fetch_page("https://h.test/1")

    dead = page_for("ProxyError: cannot connect to proxy")
    dns = page_for("ConnectError: Name or service not known")
    assert "ProxyError" in dead.note
    assert "ConnectError" in dns.note
    assert dead.note != dns.note


def test_transport_error_note_is_scrubbed():
    from certainlyscraped.fetch import FetchError

    pool = ProxyPool.from_config(
        {"proxies": ["http://u:s3cret@10.1.2.3:8000 # a"]}, env={},
        allow_userinfo=True)

    def fetch_fn(url, tier, **kwargs):
        raise FetchError(f"ProxyError: {kwargs['proxy']} refused")

    ladder = _ladder_with(fetch_fn, cfg={"max_retries": 0}, pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert "s3cret" not in page.note
    assert "10.1.2.3" not in page.note
    assert "***" in page.note


# --- fix round A: I3 -- guard errors stay inside the ladder ---------------


def test_private_address_error_is_terminal_and_keeps_the_rung():
    from certainlyscraped.netguard import PrivateAddressError

    def fetch_fn(url, tier, **kwargs):
        raise PrivateAddressError("private address: 10.0.0.1")

    ladder = _ladder_with(fetch_fn)
    page = ladder.fetch_page("https://h.test/1")
    assert classify_fetch("https://h.test/1", page.status,
                         page.html).outcome == "error"
    assert page.status == 0
    assert page.attempts == 1 == len(page.steps)
    assert page.steps == ["t1"]
    assert "PrivateAddressError" in page.note
    assert "10.0.0.1" in page.note


def test_cannot_resolve_is_a_normal_status_zero_error():
    def fetch_fn(url, tier, **kwargs):
        raise ValueError("cannot resolve no-such-host.invalid")

    ladder = _ladder_with(fetch_fn)
    page = ladder.fetch_page("https://h.test/1")
    assert classify_fetch("https://h.test/1", page.status,
                         page.html).outcome == "error"
    assert page.steps == ["t1", "t1", "t2", "t2"]
    assert page.attempts == 4 == len(page.steps)
    assert "cannot resolve" in page.note


@pytest.mark.parametrize("case", ["ok", "not_found", "blocked",
                                  "rate_limited", "error", "no_proxy",
                                  "private", "unresolvable"])
def test_attempts_always_equals_the_number_of_steps(case):
    """R43 invariant: every path that ends a page reports a coherent trail."""
    if case == "ok":
        ladder, _, _ = make([(200, OK)])
        page = ladder.fetch_page("https://h.test/1")
    elif case == "not_found":
        ladder, _, _ = make([(404, "")])
        page = ladder.fetch_page("https://h.test/1")
    elif case == "blocked":
        ladder, _, _ = make([(403, CHALLENGE)])
        page = ladder.fetch_page("https://h.test/1")
    elif case == "rate_limited":
        ladder, _, _ = make([(429, "")])
        page = ladder.fetch_page("https://h.test/1")
    elif case == "error":
        ladder, _, _ = make([(502, "bad gateway")])
        page = ladder.fetch_page("https://h.test/1")
    elif case == "no_proxy":
        pool, _ = pool_for(["http://a:1 # a"])
        pool.report("http://a:1", "h.test", "blocked")
        ladder, _, _ = make([(200, OK)], pool=pool)
        page = ladder.fetch_page("https://h.test/1")
    elif case == "private":
        from certainlyscraped.netguard import PrivateAddressError

        def fetch_fn(url, tier, **kwargs):
            raise PrivateAddressError("private address: 10.0.0.1")

        page = _ladder_with(fetch_fn).fetch_page("https://h.test/1")
    else:
        def fetch_fn(url, tier, **kwargs):
            raise ValueError("cannot resolve no-such-host.invalid")

        page = _ladder_with(fetch_fn).fetch_page("https://h.test/1")
    assert page.attempts == len(page.steps)


# --- fix round A: I4 -- pacing is per proxy, not per group ----------------


class ThreadSafeClock(FakeClock):
    """A fake clock the pool's worker threads can share."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def clock(self):
        with self._lock:
            return self.t

    def sleep(self, seconds):
        with self._lock:
            self.sleeps.append(seconds)
            self.t += seconds


def test_pacing_is_per_proxy_not_per_group():
    """I4/R44: four proxies in one /16 give four pacing slots, not one."""
    from certainlyscraped.runner import run_parallel

    pool, _ = pool_for([f"http://10.1.0.{i}:8000" for i in range(1, 5)])
    # one rotation group ("net-1"), four endpoints: the case that used to
    # share a single pacing slot.
    clock = ThreadSafeClock()

    def fetch_fn(url, tier, **kwargs):
        return 200, OK

    ladder = Ladder({"concurrency": 4, "rate_limit_sec": 1.0}, 1, pool=pool,
                    fetch_fn=fetch_fn, sleep=clock.sleep,
                    gate=RateGate(1.0, clock=clock.clock, sleep=clock.sleep))
    urls = [f"https://site.example/p{i}" for i in range(8)]
    run_parallel(ladder.fetch_page, urls,
                 {"concurrency": 4, "rate_limit_sec": 0},
                 lambda html, url: 1, gate=None)
    # Eight pages over four one-second slots is one second of pacing; one
    # shared group slot would be seven.
    assert 0.9 <= clock.t <= 2.0, clock.t


# --- fix round A: I5 -- a dead proxy fails over ---------------------------


def test_a_fetch_error_tries_another_group_before_the_same_tier_retry():
    pool, _ = pool_for(["http://a:1 # a", "http://b:1 # b"])
    calls = []

    def fetch_fn(url, tier, **kwargs):
        calls.append((tier, kwargs.get("proxy")))
        if kwargs.get("proxy") == "http://a:1":
            raise FetchError("ConnectError: connection refused")
        return 200, OK

    ladder = _ladder_with(fetch_fn, pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1@a", "t1@b"]
    assert page.attempts == 2
    assert page.status == 200
    assert calls == [(1, "http://a:1"), (1, "http://b:1")]
    # The tier came from failover, not from a climb: it is not remembered.
    assert ladder.level("h.test") == 1


def test_a_5xx_response_still_keeps_the_lease():
    """R45: a response (unlike a FetchError) is the tier's problem."""
    pool, _ = pool_for(["http://a:1 # a", "http://b:1 # b"])
    calls = []

    def fetch_fn(url, tier, **kwargs):
        calls.append((tier, kwargs.get("proxy")))
        return 500, "server error"

    ladder = _ladder_with(fetch_fn, pool=pool,
                          cfg={"max_retries": 3, "max_tier": 2})
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1@a", "t1@a", "t2@a", "t2@a"]
    assert {proxy for _, proxy in calls} == {"http://a:1"}


# --- fix round A: R46 -- backoff and group switches are visible -----------


def test_backoff_is_logged_through_the_scrubbing_log(monkeypatch):
    import io

    calls = []

    def fake_fetch(url, tier, **kwargs):
        calls.append(kwargs.get("proxy"))
        if len(calls) == 1:
            return 429, "", {}
        return 200, OK, {}

    monkeypatch.setattr(api, "_fetch", fake_fetch)
    cfg = {"tier": 1, "rate_limit_sec": 0, "cooldown_sec": 0.05,
           "max_retries": 1,
           "url": "https://h.test/1", "urls": ["https://h.test/1"],
           "proxies": ["http://u:s3cret@10.1.2.3:8000 # a"],
           "items": {"selector": "li"},
           "fields": {"t": {"selector": "a"}}}
    log = io.StringIO()
    api.scrape(cfg, log=log, proxies_from_file=True)
    text = log.getvalue()
    assert "[ladder] h.test: rate limited, waiting 0.05s" in text
    assert "10.1.2.3" not in text and "s3cret" not in text


def test_group_switch_is_logged_through_the_scrubbing_log(monkeypatch):
    import io

    calls = []

    def fake_fetch(url, tier, **kwargs):
        calls.append(kwargs.get("proxy"))
        if len(calls) == 1:
            return 429, "", {}
        return 200, OK, {}

    monkeypatch.setattr(api, "_fetch", fake_fetch)
    cfg = {"tier": 1, "rate_limit_sec": 0,
           "url": "https://h.test/1", "urls": ["https://h.test/1"],
           "proxies": ["http://u:s3cret@10.1.2.3:8000 # a",
                       "http://10.9.9.9:8000 # b"],
           "items": {"selector": "li"},
           "fields": {"t": {"selector": "a"}}}
    log = io.StringIO()
    api.scrape(cfg, log=log, proxies_from_file=True)
    text = log.getvalue()
    assert "[ladder] h.test: rate limited, trying another proxy group" in text
    assert "10.1.2.3" not in text and "s3cret" not in text


def test_steps_and_proxy_group_carry_no_hostname_or_prefix():
    """R46: a hostname proxy and two IPv4 proxies stay anonymous in the trail."""
    pool, _ = pool_for(["http://squid.internal.corp:3128",
                        "http://10.1.2.3:8000", "http://10.2.3.4:8000"])
    ladder, _, _ = make([(200, OK)], pool=pool)
    page = ladder.fetch_page("https://h.test/1")
    assert page.steps == ["t1@net-1"]
    assert page.proxy_group == "net-1"
    for text in page.steps + [page.proxy_group]:
        assert "squid" not in text and "10." not in text
        assert "/" not in text


# --- fix round A: R34 is verified end to end ------------------------------


def test_a_challenge_is_reported_to_the_pool_with_challenge_true():
    """Mutation 22: the ladder passes challenge= to pool.report."""
    pool = FakePool(["a"])
    ladder, _, _ = make([(403, CHALLENGE), (200, OK)], pool=pool)
    ladder.fetch_page("https://h.test/1")
    assert [report[0] for report in pool.reports] == ["a", "a"]
    assert pool.reports[0][2] == "blocked"
    assert pool.reports[0][3] is True
