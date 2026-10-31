"""Per-page outcome model.

Every layer passes the same two records around: ``PageFetch`` is what a fetch
ladder returns for one page, ``PageOutcome`` is the final per-page record with
its classification and row count. Classification is delegated to
:func:`certainlyscraped.classify.classify_response` so "blocked" means exactly
the same thing everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .classify import _blocked_by, classify_response

# Outcomes that make a run incomplete (exit 4) after all retries.
INCOMPLETE = frozenset({"blocked", "rate_limited", "error"})

# Below this body size a challenge marker means a block page rather than
# ordinary page content; matches classify_response's own threshold.
SMALL_BODY = 20000

_COUNT_ORDER = ("ok", "empty", "not_found", "blocked", "rate_limited", "error")


@dataclass
class PageFetch:
    """One fetched page, before extraction.

    ``outcome`` is the fetcher's own verdict when it knows one without a
    response (the ladder stopped because no proxy was available); otherwise
    the runner classifies the page from status and body.
    """

    status: int
    html: str
    attempts: int = 1
    steps: list[str] = field(default_factory=list)
    proxy_group: str | None = None
    elapsed_ms: int = 0
    note: str = ""
    outcome: str | None = None


@dataclass
class PageOutcome:
    """Final record for one page (spec section 7)."""

    url: str
    outcome: str
    status: int = 0
    rows: int = 0
    attempts: int = 1
    steps: list[str] = field(default_factory=list)
    proxy_group: str | None = None
    elapsed_ms: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "outcome": self.outcome,
            "status": self.status,
            "rows": self.rows,
            "attempts": self.attempts,
            "steps": list(self.steps),
            "proxy_group": self.proxy_group,
            "elapsed_ms": self.elapsed_ms,
            "note": self.note,
        }


@dataclass
class Verdict:
    outcome: str
    note: str = ""
    challenge: bool = False


def classify_fetch(url: str, status: int, html: str) -> Verdict:
    """Classify one page as ok | blocked | rate_limited | not_found | error.

    ``empty`` is never returned here: the caller knows the row count and turns
    an ``ok`` page with no rows into ``empty``.
    """
    text = html or ""
    probe = classify_response(url, status, text)
    small = len(text) < SMALL_BODY
    # Same evidence classify_response used: markers or a hard block signature
    # on a small body is a challenge page, not content.
    challenge = small and (bool(probe.markers) or _blocked_by(text) is not None)

    if status == 0:
        return Verdict("error", "no response")
    if status in (404, 410):
        return Verdict("not_found", f"HTTP {status}")
    # R36 / spec s4.2: a 429 is always pacing, and a 419 is pacing only when
    # it carries no challenge evidence. A 419 with markers falls through to
    # the challenge branch below, so the ladder climbs instead of rotating a
    # proxy or backing off.
    if status == 429 or (status == 419 and not challenge):
        return Verdict("rate_limited", f"HTTP {status}", challenge)
    if small and challenge:
        return Verdict("blocked", probe.note or f"HTTP {status} challenge", True)
    if status == 403 or (probe.tier == 3 and small):
        return Verdict("blocked", f"HTTP {status} blocked")
    if status >= 400:
        return Verdict("error", f"HTTP {status}")
    # Remaining 2xx/3xx: a full-size body with a block signature is a real
    # page that only needs TLS impersonation (probe.note says so).
    return Verdict("ok", probe.note)


def fetch_verdict(url: str, fetched: PageFetch | None) -> Verdict:
    """The verdict for a fetched page: the fetcher's own when it set one.

    A ladder that stopped without a response (no proxy available for this
    host) knows the page is blocked even though its status is 0; every other
    page is classified from status and body as before.
    """
    if fetched is None:
        return Verdict("error", "no response")
    if fetched.outcome:
        return Verdict(str(fetched.outcome), fetched.note or "")
    return classify_fetch(url, fetched.status, fetched.html)


def outcome_for(url: str, fetched: PageFetch | None, verdict: Verdict,
                rows: int | None = None,
                note: str | None = None) -> PageOutcome:
    """Build the per-page record; the one place ``PageOutcome`` gets filled.

    Every runner goes through here so a new field only has to be wired once.
    ``fetched`` carries attempts/steps/proxy_group/elapsed_ms (pass ``None``
    when no page came back, e.g. the fetch raised). ``rows`` is the count the
    page handler measured; leave it ``None`` for a page that was not parsed.
    An ``ok`` page with zero rows becomes ``empty``. ``note`` overrides the
    verdict's note.
    """
    if fetched is None:
        fetched = PageFetch(status=0, html="")
    outcome = PageOutcome(
        url=url, outcome=verdict.outcome, status=fetched.status,
        attempts=fetched.attempts, steps=list(fetched.steps),
        proxy_group=fetched.proxy_group, elapsed_ms=fetched.elapsed_ms,
        note=verdict.note if note is None else note)
    if rows is not None:
        outcome.rows = rows
        if outcome.outcome == "ok" and rows == 0:
            outcome.outcome = "empty"
    return outcome


def scrape_status(n_rows: int, outcomes: list[PageOutcome], min_rows: int) -> str:
    """Aggregate a run: complete | incomplete | too_few_rows | blocked | error."""
    if outcomes and n_rows == 0:
        kinds = {o.outcome for o in outcomes}
        if kinds <= {"blocked", "rate_limited"}:
            return "blocked"
        if kinds <= {"error"}:
            return "error"
    if n_rows < min_rows:
        return "too_few_rows"
    if any(o.outcome in INCOMPLETE for o in outcomes):
        return "incomplete"
    return "complete"


def _path_and_query(url: str) -> str:
    parts = urlparse(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def summary_line(outcomes: list[PageOutcome]) -> str:
    """`[done] 10 pages: 9 ok, 1 blocked (HTTP 419 on /news?p=2)`."""
    if not outcomes:
        return "[done] 0 pages"
    counts: dict[str, int] = {k: 0 for k in _COUNT_ORDER}
    for outcome in outcomes:
        counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
    listed = ", ".join(f"{counts[k]} {k}" for k in _COUNT_ORDER if counts[k])
    pages = "page" if len(outcomes) == 1 else "pages"
    line = f"[done] {len(outcomes)} {pages}: {listed}"
    first = next((o for o in outcomes if o.outcome in INCOMPLETE), None)
    if first is not None:
        where = _path_and_query(first.url)
        # A request that never got a response has no HTTP status to quote.
        cause = ("no response" if first.status == 0 else f"HTTP {first.status}")
        line += f" ({cause} on {where})"
    return line
