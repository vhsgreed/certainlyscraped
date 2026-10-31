"""Per-site escalation ladder (spec section 4).

Every page starts at the cheapest tier that works and climbs only as far as it
must: rate limits back off (or rotate a proxy group), challenge pages and
plain 403s raise the tier, and a site that needed tier 2 keeps starting there
for the rest of the run.

One ``Ladder`` per scrape. It owns three things the rest of the pipeline must
not duplicate:

* the **pacing gate** -- ``gate.wait((host, group))`` before every attempt, so
  ``run_parallel``/``follow_links`` are handed ``gate=None`` and never charge
  a page a second wait;
* the **retry budget** -- at most ``1 + max_retries`` attempts per page;
* the **sticky level** -- ``level(host)``, remembered per host for this ladder
  only, so a new ``scrape()`` starts fresh.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from .fetch import FetchError, fetch, tier3_available
from .outcome import PageFetch, classify_fetch
from .runner import RateGate

MAX_TIER = 3
MAX_RETRIES = 3
DEFAULT_MAX_TIER = 2
DEFAULT_MAX_RETRIES = 3
DEFAULT_COOLDOWN_SEC = 300

# How long one backoff slice runs before the cancel event is checked again.
CANCEL_SLICE_SEC = 0.25

# ok / not_found end the page: no retry can improve them. ("empty" is added
# later by the runner, from the row count -- the ladder never sees it.)
_TERMINAL = frozenset({"ok", "not_found"})

# A proxy has not been chosen for the next attempt (distinct from "no proxy").
_UNSET = object()


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def _number(value, name: str) -> float:
    """A finite number, or a ValueError that names the offending key.

    Failing open here is worse than failing loudly: a typo'd ``max_tier``
    silently disabled escalation, and ``.inf`` reached ``time.sleep``.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def _clamp(value, low: int, high: int, name: str = "value") -> int:
    return max(low, min(high, int(_number(value, name))))


def escalate_tier3_usable(ladder) -> bool:
    """True only when tier 3 can really be fetched: installed and unguarded.

    ``fetch`` refuses tier 3 under the MCP guard (it cannot police a browser
    hop), so the ladder must treat that as "no tier 3" rather than climbing
    into a refusal that would surface as a status-0 error.
    """
    return ladder.guard is None and tier3_available()


def _group_of(proxy) -> str | None:
    """The pool labels proxies by group; a bare string proxy has none."""
    if proxy is None:
        return None
    group = getattr(proxy, "group", None)
    return None if group is None else str(group)


def _proxy_url(proxy) -> str | None:
    if proxy is None:
        return None
    if isinstance(proxy, str):
        return proxy
    return getattr(proxy, "url", None)


def _retry_after(headers: dict | None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date)."""
    value = (headers or {}).get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(str(value).strip())
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class Ladder:
    """Escalating fetcher: one :class:`PageFetch` per page, cheapest tier first."""

    def __init__(self, cfg: dict, start_tier: int, pool=None, gate=None,
                 fetch_fn=fetch, sleep=time.sleep, guard=None, log=None,
                 cancel: threading.Event | None = None) -> None:
        cfg = cfg or {}
        self.cfg = cfg
        self.guard = guard
        # Set by run_parallel when the run is interrupted; a backoff then
        # returns early instead of parking a pool worker for cooldown_sec.
        self.cancel = cancel
        self.start_tier = _clamp(start_tier, 1, MAX_TIER, "start_tier")
        self.max_tier = _clamp(cfg.get("max_tier", DEFAULT_MAX_TIER),
                               1, MAX_TIER, "max_tier")
        self.max_retries = _clamp(cfg.get("max_retries", DEFAULT_MAX_RETRIES),
                                  0, MAX_RETRIES, "max_retries")
        if self.start_tier == MAX_TIER and not escalate_tier3_usable(self):
            # A probed/config tier 3 we cannot run starts at 2 instead; the
            # ladder then stops with "tier 3 not available" if 2 is blocked.
            self.start_tier = MAX_TIER - 1
        self.cooldown_sec = max(0.0, _number(
            cfg.get("cooldown_sec", DEFAULT_COOLDOWN_SEC), "cooldown_sec"))
        self.pool = pool
        self.fetch_fn = fetch_fn
        self.sleep = sleep
        self.log = log
        self.timeout = cfg.get("timeout", 30)
        self.cookies = cfg.get("cookies")
        # Single-proxy configs stay supported: no pool means the configured
        # proxy is used for every attempt.
        self.proxy = cfg.get("proxy")
        # The ladder is the only pacer: run_parallel/follow_links get
        # gate=None so one attempt is never waited for twice.
        self.gate = gate if gate is not None else RateGate(
            cfg.get("per_proxy_rate_sec", cfg.get("rate_limit_sec", 1.0)),
            sleep=sleep)
        self._lock = threading.Lock()
        self._levels: dict[str, int] = {}

    # -- state -------------------------------------------------------------

    def level(self, host: str) -> int:
        """The tier a new page on ``host`` starts at (sticky, per ladder)."""
        with self._lock:
            return self._levels.get(host, self.start_tier)

    def _remember(self, host: str, tier: int) -> None:
        with self._lock:
            if tier > self._levels.get(host, self.start_tier):
                self._levels[host] = tier

    def _cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def _backoff_sleep(self, seconds: float) -> bool:
        """Wait ``seconds``; True when the run was cancelled while waiting.

        Without a cancel event this is the single ``sleep`` call it always
        was (so a fake clock records one wait per backoff). With one, the
        wait runs in slices of at most :data:`CANCEL_SLICE_SEC` so Ctrl-C
        does not wait out a 300 s cooldown on a pool worker thread.
        """
        if self.cancel is None:
            self.sleep(seconds)
            return False
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if self.cancel.is_set():
                return True
            slice_sec = min(CANCEL_SLICE_SEC, remaining)
            self.sleep(slice_sec)
            remaining -= slice_sec
        return self.cancel.is_set()

    # -- one page ----------------------------------------------------------

    def fetch_page(self, url: str) -> PageFetch:
        host = _host(url)
        started = time.monotonic()
        tier = min(self.level(host), self.max_tier)
        steps: list[str] = []
        status, html = 0, ""
        verdict = None
        note = ""
        proxy = None
        group: str | None = None
        tried_groups: set[str] = set()
        next_proxy = _UNSET
        error_retried = False
        backoff_used = False
        limit = 1 + self.max_retries

        while len(steps) < limit:
            # A first attempt already submitted still runs; after that an
            # interrupted run stops the page rather than fetching again.
            if steps and self._cancelled():
                break
            if tier == MAX_TIER and not escalate_tier3_usable(self):
                note = "tier 3 not available"
                break

            if next_proxy is not _UNSET:
                proxy = next_proxy
                next_proxy = _UNSET
            elif self.pool is not None:
                if not tried_groups:
                    proxy = self.pool.acquire(host) or proxy
                else:
                    proxy = self.pool.alternate(host, set(tried_groups)) or proxy

            group = _group_of(proxy)
            if group is not None:
                tried_groups.add(group)
            steps.append(f"t{tier}" + (f"@{group}" if group else ""))

            self.gate.wait((host, group))
            status, html, headers = self._call(url, tier, proxy)
            verdict = classify_fetch(url, status, html)
            self._report(proxy, host, verdict.outcome)

            if verdict.outcome in _TERMINAL:
                break

            if verdict.outcome == "rate_limited":
                alternate = (self.pool.alternate(host, set(tried_groups))
                             if self.pool is not None else None)
                if alternate is not None:
                    next_proxy = alternate
                    continue
                # Spec 4.2: ONE backoff per page. Retry the same tier once
                # after waiting, and take the 429 as final if it comes back
                # -- a second cooldown buys nothing (and no wait is spent on
                # an attempt budget that is already used up).
                if backoff_used or len(steps) >= limit:
                    break
                backoff_used = True
                if self._backoff_sleep(self._backoff(headers)):
                    break  # Ctrl-C: stop the page, keep the 429 verdict
                continue

            if verdict.outcome == "blocked":
                if not verdict.challenge and self.pool is not None:
                    alternate = self.pool.alternate(host, set(tried_groups))
                    if alternate is not None:
                        next_proxy = alternate
                        continue
                reason = "challenge" if verdict.challenge else "blocked"
                climbed, note = self._climb(url, tier, reason)
                if climbed is None:
                    break
                tier = climbed
                tried_groups, error_retried = set(), False
                continue

            if verdict.outcome == "error":
                if not error_retried:
                    # One same-tier retry (a reset connection, not a wall).
                    error_retried = True
                    continue
                climbed, note = self._climb(url, tier, "error")
                if climbed is None:
                    break
                tier = climbed
                tried_groups, error_retried = set(), False
                continue

            break  # unknown outcome: do not loop on it

        self._remember(host, tier)
        elapsed = int((time.monotonic() - started) * 1000)
        return PageFetch(status=status, html=html, attempts=len(steps),
                         steps=steps, proxy_group=group, elapsed_ms=elapsed,
                         note=note)

    # -- helpers -----------------------------------------------------------

    def _climb(self, url: str, tier: int, reason: str):
        """(next tier, "") or (None, note explaining why there is no next)."""
        if tier >= self.max_tier:
            return None, f"max_tier {self.max_tier} reached"
        nxt = tier + 1
        if nxt >= MAX_TIER and not escalate_tier3_usable(self):
            return None, "tier 3 not available"
        if self.log:
            print(f"[ladder] {url}: t{tier} -> t{nxt} ({reason})", file=self.log)
        return nxt, ""

    def _backoff(self, headers: dict | None) -> float:
        delay = _retry_after(headers)
        if delay is None:
            return self.cooldown_sec
        return min(delay, self.cooldown_sec)

    def _call(self, url: str, tier: int, proxy):
        proxy_url = _proxy_url(proxy)
        if proxy_url is None and self.pool is None:
            proxy_url = self.proxy
        try:
            result = self.fetch_fn(
                url, tier, timeout=self.timeout, proxy=proxy_url,
                cookies=self.cookies, guard=self.guard, return_headers=True)
        except FetchError as exc:
            return 0, "", {"x-fetch-error": str(exc)}
        if len(result) == 3:
            status, html, headers = result
            return status, html, headers or {}
        status, html = result
        return status, html, {}

    def _report(self, proxy, host: str, outcome: str) -> None:
        if self.pool is not None and proxy is not None:
            self.pool.report(proxy, host, outcome)
