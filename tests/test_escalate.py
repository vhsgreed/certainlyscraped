"""Escalation ladder (spec section 4). No network: every fetch is scripted.

Run: python3 -m pytest tests/ -q
"""
import sys

sys.path.insert(0, ".")

import pytest

import certainlyscraped.api as api
import certainlyscraped.escalate as escalate
from certainlyscraped.escalate import Ladder
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
    ladder, _, _ = make([(429, "")], cfg={"max_retries": 3})
    page = ladder.fetch_page("https://h.test/1")
    assert page.attempts == 4
    assert page.steps == ["t1"] * 4


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

    def report(self, proxy, host, outcome):
        self.reports.append((getattr(proxy, "group", None), host, outcome))


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
            stop_on_trailing=False, gate="missing"):
        captured["gate"] = gate
        return real(fetch_page, urls, cfg, on_page, log=log,
                    stop_on_trailing=stop_on_trailing, gate=gate)

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
