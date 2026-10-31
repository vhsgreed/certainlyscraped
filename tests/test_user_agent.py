"""Tier-1 User-Agent tests (no network). Run: python3 -m pytest tests/ -q"""
import sys

sys.path.insert(0, ".")

import httpx

from certainlyscraped import __version__
from certainlyscraped import classify, fetch as fetch_mod


class _R:
    status_code = 200
    text = "<html><body>ok</body></html>"


def _capture(monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen["ua"] = kw["headers"]["User-Agent"]
        return _R()
    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


def test_tier1_fetch_sends_honest_user_agent(monkeypatch):
    seen = _capture(monkeypatch)
    fetch_mod.fetch("https://example.test/", 1)
    assert seen["ua"].startswith(f"certainlyscraped/{__version__} ")
    assert "github.com/vhsgreed/certainlyscraped" in seen["ua"]
    assert "Mozilla" not in seen["ua"]


def test_probe_uses_same_user_agent_as_tier1_fetch(monkeypatch):
    # The probe decides whether tier 1 works, so it must test the UA tier 1 sends.
    seen = _capture(monkeypatch)
    classify._probe("https://example.test/", 10)
    assert seen["ua"] == fetch_mod.USER_AGENT
