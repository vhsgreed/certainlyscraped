"""Per-site escalation ladder (spec section 4).

Every page starts at the cheapest tier that works and climbs only as far as it
must: rate limits back off (or rotate a proxy group), challenge pages and
plain 403s raise the tier, and a site that needed tier 2 keeps starting there
for the rest of the run.

One ``Ladder`` per scrape. It owns three things the rest of the pipeline must
not duplicate:

* the **pacing gate** -- ``gate.wait((host, proxy key))`` before every
  attempt, so two proxies to one host each get their own slot and
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

from .fetch import FetchError, fetch, redact, tier3_available
from .netguard import PrivateAddressError
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


def _proxies_block(cfg: dict) -> dict:
    """The ``proxies`` mapping, when the config uses one (spec section 5)."""
    block = cfg.get("proxies")
    return block if isinstance(block, dict) else {}


def _tuned(cfg: dict, key: str, default):
    """A value from the ``proxies`` block first, then the top level.

    Spec s5 puts ``per_proxy_rate_sec``/``max_retries``/``cooldown_sec`` in
    the same block as the proxy list; the top-level key still works.
    """
    block = _proxies_block(cfg)
    if key in block:
        return block[key]
    return cfg.get(key, default)


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
        self.max_retries = _clamp(_tuned(cfg, "max_retries", DEFAULT_MAX_RETRIES),
                                  0, MAX_RETRIES, "max_retries")
        if self.start_tier == MAX_TIER and not escalate_tier3_usable(self):
            # A probed/config tier 3 we cannot run starts at 2 instead; the
            # ladder then stops with "tier 3 not available" if 2 is blocked.
            self.start_tier = MAX_TIER - 1
        self.cooldown_sec = max(0.0, _number(
            _tuned(cfg, "cooldown_sec", DEFAULT_COOLDOWN_SEC), "cooldown_sec"))
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
            _tuned(cfg, "per_proxy_rate_sec",
                   cfg.get("rate_limit_sec", 1.0)),
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
        group: str | None = None
        tried_groups: set[str] = set()
        error_retried = False
        error_switched = False
        backoff_used = False
        forced = None  # an outcome the ladder knows without a response
        last_error: str | None = None
        # R45: a tier reached only by climbing on an error is not sticky.
        remember = True
        limit = 1 + self.max_retries

        def finish() -> PageFetch:
            if remember:
                self._remember(host, tier)
            # R42: a page that ended without a response says why it stopped
            # (the last transport error), not the generic climb note. A climb
            # note survives only when the final verdict came from a response.
            final_note = note
            if status == 0 and last_error:
                final_note = last_error
            elapsed = int((time.monotonic() - started) * 1000)
            return PageFetch(status=status, html=html, attempts=len(steps),
                             steps=steps, proxy_group=group,
                             elapsed_ms=elapsed, note=self.scrub(final_note),
                             outcome=forced)

        # R33: a page leases one proxy for its whole life. The pool's park
        # (the cooldown a walled proxy earned for this host) outlives the
        # page and steers the *next* one; inside this page the tier, not the
        # proxy, is what the ladder is changing, so the lease is kept across
        # retries and climbs. With a pool configured there is no fetch
        # without a lease: no proxy means no page, never the origin IP.
        if self.pool is not None:
            proxy = self.pool.acquire(host)
            if proxy is None:
                note = f"no proxy available for {host}"
                forced = "blocked"
                return finish()
            group = _group_of(proxy)
            if group is not None:
                tried_groups.add(group)
        else:
            proxy = None

        while len(steps) < limit:
            # A first attempt already submitted still runs; after that an
            # interrupted run stops the page rather than fetching again.
            if steps and self._cancelled():
                break
            if tier == MAX_TIER and not escalate_tier3_usable(self):
                note = "tier 3 not available"
                break

            group = _group_of(proxy)
            if group is not None:
                tried_groups.add(group)
            steps.append(f"t{tier}" + (f"@{group}" if group else ""))

            self.gate.wait(self._pacing_key(host, proxy))
            status, html, headers, error, terminal = self._call(url, tier,
                                                                proxy)
            if error:
                last_error = error
            verdict = classify_fetch(url, status, html)

            if terminal:
                # R43: a policy refusal is final for this page -- no retry,
                # no climb, no fetch from another rung or proxy.
                forced = "error"
                break

            self._report(proxy, host, verdict)

            if verdict.outcome in _TERMINAL:
                break

            if verdict.outcome == "rate_limited":
                # Spec 4.2 rule 2: a different group first. No other group
                # left means this is the proxy's pace, not the whole pool's
                # problem -- keep the lease and back off once.
                switched = self._alternate(host, tried_groups, "rate limited")
                if switched is not None:
                    proxy = switched
                    continue
                # ONE backoff per page: retry the same tier once after
                # waiting, and take the 429 as final if it comes back (a
                # second cooldown buys nothing, and no wait is spent on an
                # attempt budget that is already used up).
                if backoff_used or len(steps) >= limit:
                    break
                backoff_used = True
                delay = self._backoff(headers)
                if self.log:
                    # R46: a user must not see a silent hang.
                    print(f"[ladder] {host}: rate limited, waiting {delay:g}s",
                          file=self.log)
                if self._backoff_sleep(delay):
                    break  # Ctrl-C: stop the page, keep the 429 verdict
                continue

            if verdict.outcome == "blocked":
                # Spec 4.2 rule 4: a plain 403 buys one other group before a
                # heavier tier. A challenge is not the proxy's fault (rule
                # 3): climb on the lease.
                if not verdict.challenge:
                    switched = self._alternate(host, tried_groups, "blocked")
                    if switched is not None:
                        proxy = switched
                        continue
                reason = "challenge" if verdict.challenge else "blocked"
                climbed, note = self._climb(url, tier, reason)
                if climbed is None:
                    break
                tier = climbed
                error_retried = False
                continue

            if verdict.outcome == "error":
                # R45 (amends R33): no response at all is the proxy's
                # problem -- one alternate() before the same-tier retry. A
                # 5xx *is* a response, so it keeps the lease.
                if (last_error is not None and self.pool is not None
                        and not error_switched):
                    error_switched = True
                    switched = self._alternate(host, tried_groups, "error")
                    if switched is not None:
                        proxy = switched
                        continue
                if not error_retried:
                    # One same-tier retry (a reset connection, not a wall).
                    error_retried = True
                    continue
                climbed, note = self._climb(url, tier, "error")
                if climbed is None:
                    break
                tier = climbed
                remember = False
                error_retried = False
                continue

            break  # unknown outcome: do not loop on it

        return finish()

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
        """(status, html, headers, error, terminal) for one attempt.

        ``error`` is the transport failure's text (None when a response
        arrived). ``terminal`` is True for a policy refusal -- R43: the
        guard's PrivateAddressError ends the page, it is not retried or
        climbed. The guard's resolve failure is an ordinary status-0 error
        so the page still gets its retries and rungs.
        """
        proxy_url = _proxy_url(proxy)
        if proxy_url is None and self.pool is None:
            proxy_url = self.proxy
        try:
            result = self.fetch_fn(
                url, tier, timeout=self.timeout, proxy=proxy_url,
                cookies=self.cookies, guard=self.guard, return_headers=True)
        except PrivateAddressError as exc:
            # Name the class: a policy refusal is a different diagnosis from
            # a transport failure, and the note is what an operator reads.
            return 0, "", {}, f"{type(exc).__name__}: {exc}", True
        except FetchError as exc:
            return 0, "", {}, str(exc), False
        except ValueError as exc:
            if not str(exc).startswith("cannot resolve"):
                raise
            return 0, "", {}, f"{type(exc).__name__}: {exc}", False
        if len(result) == 3:
            status, html, headers = result
            return status, html, headers or {}, None, False
        status, html = result
        return status, html, {}, None, False

    def _pacing_key(self, host: str, proxy):
        """R44: one pacing slot per (host, endpoint), never per group.

        A real pool knows each endpoint's identity; a test double without
        ``key``/``_key`` keeps the old per-group behaviour.
        """
        if self.pool is None:
            return (host, None)
        identify = getattr(self.pool, "key", None) \
            or getattr(self.pool, "_key", None)
        if identify is None:
            return (host, _group_of(proxy))
        return (host, identify(proxy))

    def _alternate(self, host: str, tried_groups: set[str], reason: str):
        """One proxy from a group not tried yet for this page, else None.

        Called once per network-level verdict (a 429, a plain 403, a
        transport error) and only ever to *move* the lease; returning None
        means the page keeps the proxy it holds and follows the normal rule
        for that verdict (R33). R46: a switch is logged, so a run that
        rotates visibly says why.
        """
        if self.pool is None:
            return None
        alternate = getattr(self.pool, "alternate", None)
        if alternate is None:
            return None
        switched = alternate(host, set(tried_groups))
        if switched is not None and self.log:
            print(f"[ladder] {host}: {reason}, trying another proxy group",
                  file=self.log)
        return switched

    def _report(self, proxy, host: str, verdict) -> None:
        """Hand the verdict to the pool, challenge included.

        R34: only network-level verdicts park a (proxy, host) pair. A
        challenge proves the proxy reached the site, so it is the site that
        is walled, not the endpoint -- the pool leaves that pair alone.
        """
        if self.pool is None or proxy is None:
            return
        # The documented pool interface is report(proxy, host, outcome); the
        # challenge keyword is an extension. A third-party or test pool that
        # implements only the documented form must not raise TypeError from
        # deep inside the ladder.
        try:
            self.pool.report(proxy, host, verdict.outcome,
                             challenge=bool(verdict.challenge))
        except TypeError:
            self.pool.report(proxy, host, verdict.outcome)

    def scrub(self, text: str) -> str:
        """Page notes go on to logs and datasets: no proxy credential there.

        The pool knows its secrets (= passwords and session tokens) and, for
        a list pool, its own addresses; a bare string proxy has neither, so
        the generic credential mask in :func:`fetch.redact` still applies.
        """
        if not text:
            return text
        scrub = getattr(self.pool, "scrub", None) if self.pool is not None \
            else None
        if scrub is not None:
            return scrub(text)
        secrets = getattr(self.pool, "secrets", None) if self.pool is not None \
            else None
        return redact(text, secrets() if secrets is not None else ())
