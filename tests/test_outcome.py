"""Per-page outcome model: classification, status and summary line."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from certainlyscraped.outcome import (  # noqa: E402
    INCOMPLETE,
    PageFetch,
    PageOutcome,
    Verdict,
    classify_fetch,
    outcome_for,
    scrape_status,
    summary_line,
)

FIXTURES = Path(__file__).parent / "fixtures"


def books_html() -> str:
    return (FIXTURES / "books.html").read_text()


def page(url: str, outcome: str, status: int, **kw) -> PageOutcome:
    return PageOutcome(url=url, outcome=outcome, status=status, **kw)


@pytest.mark.parametrize(
    "status,body,outcome,challenge",
    [
        (200, "BOOKS", "ok", False),
        (404, "", "not_found", False),
        (410, "", "not_found", False),
        (429, "slow down", "rate_limited", False),
        (419, "Sorry", "rate_limited", False),
        # R36: a 419 carrying challenge evidence is a challenge, not pacing;
        # only a 419 without markers stays rate_limited (spec s4.2).
        (419, "<html><title>Just a moment...</title><body>"
              "cf-chl challenge</body></html>", "blocked", True),
        (403, "<title>Access Denied</title>", "blocked", True),
        (403, "nope", "blocked", False),
        (
            503,
            "<html><head><title>Just a moment</title></head><body>"
            "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'>"
            "</script></body></html>",
            "blocked",
            True,
        ),
        (500, "oops", "error", False),
        (0, "", "error", False),
        (200, "A" * 5000 + '<div class="g-recaptcha"></div>', "blocked", True),
    ],
)
def test_classify_fetch(status, body, outcome, challenge):
    if body == "BOOKS":
        body = books_html()
    verdict = classify_fetch("https://example.com/page", status, body)
    assert isinstance(verdict, Verdict)
    assert (verdict.outcome, verdict.challenge) == (outcome, challenge)


def test_classify_fetch_full_size_block_is_ok_with_tls_note():
    body = "A" * 30000 + '<script src="https://challenges.cloudflare.com/x"></script>'
    verdict = classify_fetch("https://example.com/", 200, body)
    assert verdict.outcome == "ok"
    assert verdict.challenge is False
    assert "TLS impersonation" in verdict.note


def test_classify_fetch_never_returns_empty():
    # `empty` is decided by the caller from the row count, never here.
    assert classify_fetch("https://example.com/", 200, "<html></html>").outcome == "ok"


def test_status_complete():
    outcomes = [
        page("https://x/1", "ok", 200, rows=15),
        page("https://x/2", "ok", 200, rows=15),
        page("https://x/3", "not_found", 404),
    ]
    assert scrape_status(30, outcomes, 10) == "complete"


def test_status_incomplete():
    outcomes = [page("https://x/1", "ok", 200, rows=30), page("https://x/2", "rate_limited", 429)]
    assert scrape_status(30, outcomes, 10) == "incomplete"


def test_status_too_few_wins():
    outcomes = [page("https://x/1", "ok", 200, rows=3), page("https://x/2", "blocked", 403)]
    assert scrape_status(3, outcomes, 10) == "too_few_rows"


def test_status_blocked():
    assert scrape_status(0, [page("https://x/1", "blocked", 403)], 10) == "blocked"


def test_status_error():
    assert scrape_status(0, [page("https://x/1", "error", 500)], 10) == "error"


def test_empty_not_incomplete():
    outcomes = [
        page("https://x/1", "ok", 200, rows=30),
        page("https://x/2", "empty", 200),
        page("https://x/3", "empty", 200),
    ]
    assert scrape_status(30, outcomes, 10) == "complete"
    assert "empty" not in INCOMPLETE


def test_summary_line():
    outcomes = [page(f"https://news.ycombinator.com/news?p={n}", "ok", 200, rows=10)
                for n in range(1, 10)]
    outcomes.append(
        page("https://news.ycombinator.com/news?p=2", "blocked", 419)
    )
    assert summary_line(outcomes) == (
        "[done] 10 pages: 9 ok, 1 blocked (HTTP 419 on /news?p=2)"
    )


def test_summary_line_singular_and_no_incomplete():
    assert summary_line([page("https://x/", "ok", 200, rows=1)]) == "[done] 1 page: 1 ok"


def test_summary_line_counts_in_fixed_order():
    outcomes = [
        page("https://x/a", "not_found", 404),
        page("https://x/b", "ok", 200),
        page("https://x/c", "error", 500),
        page("https://x/d", "empty", 200),
    ]
    assert summary_line(outcomes) == (
        "[done] 4 pages: 1 ok, 1 empty, 1 not_found, 1 error "
        "(HTTP 500 on /c)"
    )


def test_outcome_for_copies_fetch_details():
    fetched = PageFetch(200, "<html></html>", attempts=2, steps=["t1", "t2"],
                        proxy_group="dc-a", elapsed_ms=42)
    verdict = classify_fetch("https://x/1", 200, "<html></html>")
    out = outcome_for("https://x/1", fetched, verdict, rows=3)
    assert (out.url, out.outcome, out.status) == ("https://x/1", "ok", 200)
    assert out.rows == 3
    assert (out.attempts, out.steps, out.proxy_group, out.elapsed_ms) == \
        (2, ["t1", "t2"], "dc-a", 42)
    assert out.note == verdict.note
    assert out.steps is not fetched.steps  # copied, not shared


def test_outcome_for_zero_rows_on_ok_is_empty():
    fetched = PageFetch(200, "<html></html>")
    verdict = classify_fetch("https://x/1", 200, "<html></html>")
    assert outcome_for("https://x/1", fetched, verdict, rows=0).outcome == "empty"


def test_outcome_for_does_not_empty_a_failed_page():
    fetched = PageFetch(404, "")
    verdict = classify_fetch("https://x/1", 404, "")
    out = outcome_for("https://x/1", fetched, verdict)
    assert (out.outcome, out.rows) == ("not_found", 0)


def test_outcome_for_without_a_fetch_is_a_status_zero_error():
    out = outcome_for("https://x/1", None, Verdict("error", "FetchError: boom"))
    assert (out.status, out.attempts, out.steps, out.rows) == (0, 1, [], 0)
    assert out.note == "FetchError: boom"


def test_outcome_for_note_override_wins():
    fetched = PageFetch(429, "slow")
    verdict = classify_fetch("https://x/1", 429, "slow")
    out = outcome_for("https://x/1", fetched, verdict, note="rewritten")
    assert out.note == "rewritten"


def test_page_fetch_defaults():
    fetch = PageFetch(200, "<html></html>")
    assert fetch.attempts == 1
    assert fetch.steps == []
    assert fetch.proxy_group is None
    assert fetch.elapsed_ms == 0
    assert fetch.note == ""


def test_page_outcome_to_dict_keys_and_order():
    outcome = PageOutcome(
        url="https://x/1",
        outcome="blocked",
        status=403,
        rows=0,
        attempts=2,
        steps=["t1", "t2"],
        proxy_group="dc-a",
        elapsed_ms=840,
        note="blocked: akamai",
    )
    d = outcome.to_dict()
    assert list(d) == [
        "url", "outcome", "status", "rows", "attempts",
        "steps", "proxy_group", "elapsed_ms", "note",
    ]
    assert d["steps"] == ["t1", "t2"]
    assert d["proxy_group"] == "dc-a"
    # json-serialisable, and the steps list is not shared with the dataclass
    import json
    json.dumps(d)
    d["steps"].append("t3")
    assert outcome.steps == ["t1", "t2"]
