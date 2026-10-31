"""Proxy pool: groups, per-host cooldown, rotation and gateway templates.

A config names its proxies in one of three shapes; all of them become one
:class:`ProxyPool` that the ladder asks for a proxy per attempt::

    proxy: http://u:p@10.1.2.3:8000        # one proxy, group "default"

    proxies:                               # a list, label after "#"
      - http://u:p@10.1.2.3:8000 # dc-a
      - http://10.1.9.9:8000               # group "10.1/16"

    proxies:                               # group -> urls, or a file, or a gateway
      dc-a: [http://u:p@10.1.2.3:8000]
      list: proxies.txt
      gateway:
        template: "http://{user}-{session}:{pass}@{host}:{port}"
        host: gw.example
        user: me
        password_env: GATEWAY_PASSWORD
        session: rotate                    # or sticky
        label: gw

Groups exist to make the ladder's guesses cheap: a 403 or a 429 is retried from
a *different group* before a heavier tier is paid for, and a proxy that was
walled for one host is parked for that host only.

Two invariants hold everywhere in this module:

* **A proxy's address is never in a repr, a log line or an outcome.** ``repr``
  and ``str`` of :class:`Proxy` show the group only, and the password exists
  only inside ``Proxy.url`` -- which the fetcher consumes, never prints.
* **Passwords come from the environment**, named by ``password_env`` in the
  config. A missing variable is a ``ValueError`` that names the *variable*.
"""
from __future__ import annotations

import ipaddress
import os
import re
import secrets as _secrets
import string
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .fetch import redact

DEFAULT_COOLDOWN_SEC = 300
DEFAULT_GROUP = "default"
DEFAULT_GATEWAY_GROUP = "gateway"
ENV_PROXIES = "CERTAINLYSCRAPED_PROXIES"

# Outcomes that park a (proxy, host) pair. Anything else (ok, not_found) is a
# vote of confidence and clears the cooldown.
COOLING_OUTCOMES = frozenset({"blocked", "rate_limited", "error"})

SESSION_BYTES = 4  # token_hex(4) -> 8 hex chars
# Rotate tokens kept for redaction: enough for anything still in flight.
MAX_TOKEN_SECRETS = 512

# "url # label" / "url #label": the label is the group name.
_LABEL = re.compile(r"\s+#\s*")
_URL_PASSWORD = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/:@]*:([^@]*)@")

_GATEWAY_KEYS = frozenset({"template", "url", "password_env", "session",
                           "label", "password"})

_FORMATTER = string.Formatter()


@dataclass(frozen=True)
class Proxy:
    """One proxy endpoint. ``url`` is the only place credentials live."""

    url: str
    group: str

    def __repr__(self) -> str:  # never the address, never the credentials
        return f"Proxy(group={self.group})"

    def __str__(self) -> str:
        return repr(self)


class Gateway:
    """A provider gateway: one URL per session, built at acquire time.

    ``template`` is formatted with the config's own scalar keys (host, port,
    user, country, ...) plus ``session`` (the generated token) and ``pass`` /
    ``password`` (from ``password_env``).
    """

    def __init__(self, cfg: dict, env) -> None:
        if not isinstance(cfg, dict):
            raise ValueError("gateway must be a mapping")
        template = cfg.get("template") or cfg.get("url")
        if not isinstance(template, str) or "{" not in template:
            raise ValueError("gateway: template must be a URL string with "
                             "{placeholders}")
        if "password" in cfg:
            raise ValueError("gateway: set password_env, not password -- the "
                             "password is read from the environment and never "
                             "from the config")
        env_name = cfg.get("password_env")
        if not isinstance(env_name, str) or not env_name:
            raise ValueError("gateway: password_env is required (the name of "
                             "the environment variable holding the password)")
        if env_name not in env:
            raise ValueError(f"gateway: environment variable {env_name} is "
                             "not set")
        self.password = str(env[env_name])
        self.template = template
        self.label = str(cfg.get("label") or DEFAULT_GATEWAY_GROUP)
        self.session = str(cfg.get("session", "rotate")).strip().lower()
        if self.session not in ("rotate", "sticky"):
            raise ValueError("gateway: session must be rotate or sticky, "
                             f"got {self.session!r}")
        self.fields = {str(key): str(value) for key, value in cfg.items()
                       if str(key) not in _GATEWAY_KEYS}
        known = set(self.fields) | {"session", "pass", "password"}
        missing = sorted(set(_placeholders(template)) - known)
        if missing:
            raise ValueError(
                "gateway: template placeholders with no value: "
                + ", ".join("{" + name + "}" for name in missing))

    def build(self, token: str) -> str:
        values = dict(self.fields)
        values["session"] = token
        values["pass"] = self.password
        values["password"] = self.password
        return self.template.format(**values)


def _placeholders(template: str) -> list[str]:
    """Field names in a ``str.format`` template ('{a}-{b}' -> ['a', 'b'])."""
    names = []
    for _, field, _, _ in _FORMATTER.parse(template):
        if field:
            names.append(field.split(".")[0].split("[")[0])
    return names


class ProxyPool:
    """Thread-safe pool: round-robin over groups, cooldown per (proxy, host)."""

    def __init__(self, groups: dict[str, list[str]], gateways=(),
                 *, cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
                 clock=time.monotonic, token_factory=None) -> None:
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self._clock = clock
        self._token = token_factory or (lambda: _secrets.token_hex(SESSION_BYTES))
        self._lock = threading.Lock()
        self._groups: dict[str, list[str]] = {}
        self._gateways: dict[str, Gateway] = {}
        # Passwords live for the pool's lifetime; rotate tokens are only
        # useful while a message carrying one can still be in flight, so the
        # newest MAX_TOKEN_SECRETS are enough to redact with.
        self._passwords: list[str] = []
        self._tokens: list[str] = []
        for label, urls in groups.items():
            urls = [str(url) for url in urls]
            if not urls:
                continue
            self._groups[str(label)] = urls
            for url in urls:
                _append(self._passwords, _password_of(url))
        for gateway in gateways:
            if gateway.label in self._groups or gateway.label in self._gateways:
                raise ValueError("proxy group label is used twice: "
                                 f"{gateway.label!r}")
            self._gateways[gateway.label] = gateway
            _append(self._passwords, gateway.password)
        self._order = list(self._groups) + list(self._gateways)
        self._turn = {label: 0 for label in self._order}
        self._rr = 0
        self._cooling: dict[tuple, float] = {}
        self._sticky: dict[tuple[str, str], str] = {}

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict, env=None, base_dir: str = ".",
                    *, clock=time.monotonic, token_factory=None):
        """The pool a config asks for, or None when it names no proxy.

        ``CERTAINLYSCRAPED_PROXIES`` (a newline/comma separated list) replaces
        the config's ``proxies`` value when it is set.
        """
        cfg = cfg or {}
        env = os.environ if env is None else env
        raw = cfg.get("proxies")
        override = (env or {}).get(ENV_PROXIES) if env is not None else None
        if override:
            raw = [part.strip() for part in re.split(r"[,\n]", override)
                   if part.strip()]
        default_group = None
        if raw is None:
            single = cfg.get("proxy")
            if single:
                raw = [str(single)]
                # A single `proxy:` is one group of one: "default" (it names
                # no network, so a /16 prefix would be meaningless there).
                default_group = DEFAULT_GROUP
        if raw is None:
            return None
        entries, gateways = _read(raw, env, base_dir)
        groups: dict[str, list[str]] = {}
        for url, label in entries:
            group = label or default_group or _derive_group(url)
            groups.setdefault(group, []).append(url)
        built = [Gateway(spec, env) for spec in gateways]
        if not groups and not built:
            return None
        return cls(groups, built, cooldown_sec=_cooldown(cfg), clock=clock,
                   token_factory=token_factory)

    # -- queries -----------------------------------------------------------

    def groups(self) -> list[str]:
        with self._lock:
            return list(self._order)

    def secrets(self) -> list[str]:
        """Passwords and generated session tokens, for redaction."""
        with self._lock:
            return self._passwords + self._tokens

    def scrub(self, text: str) -> str:
        """Redact this pool's secrets *and* its own proxy addresses."""
        if not text:
            return text
        with self._lock:
            targets = self._passwords + self._tokens
            for urls in self._groups.values():
                for url in urls:
                    targets.append(url)
                    netloc = _netloc(url)
                    if netloc:
                        targets.append(netloc)
        return redact(text, targets)

    # -- handing out -------------------------------------------------------

    def acquire(self, host: str) -> Proxy | None:
        with self._lock:
            return self._issue(host, frozenset())

    def alternate(self, host: str, exclude_groups) -> Proxy | None:
        with self._lock:
            return self._issue(host, frozenset(exclude_groups or ()))

    def report(self, proxy, host: str, outcome: str) -> None:
        """Blocked/rate_limited/error park the pair; ok clears it."""
        if proxy is None:
            return
        key = self._key(proxy)
        with self._lock:
            if outcome in COOLING_OUTCOMES and self.cooldown_sec > 0:
                self._cooling[(key, host)] = self._clock() + self.cooldown_sec
            else:
                self._cooling.pop((key, host), None)

    # -- internals ---------------------------------------------------------

    def _issue(self, host: str, exclude: frozenset) -> Proxy | None:
        label = self._next_group(host, exclude)
        if label is None:
            return None
        gateway = self._gateways.get(label)
        if gateway is not None:
            return Proxy(gateway.build(self._token_for(host, gateway)), label)
        urls = self._groups[label]
        now = self._clock()
        start = self._turn[label]
        for step in range(len(urls)):
            index = (start + step) % len(urls)
            if self._cooling_for(("url", urls[index]), host, now):
                continue
            self._turn[label] = (index + 1) % len(urls)
            return Proxy(urls[index], label)
        return None  # unreachable: _next_group checked availability

    def _next_group(self, host: str, exclude: frozenset) -> str | None:
        count = len(self._order)
        if not count:
            return None
        now = self._clock()
        for step in range(count):
            index = (self._rr + step) % count
            label = self._order[index]
            if label in exclude or not self._available(label, host, now):
                continue
            self._rr = (index + 1) % count
            return label
        return None

    def _available(self, label: str, host: str, now: float) -> bool:
        if label in self._gateways:
            return not self._cooling_for(("gateway", label), host, now)
        for url in self._groups[label]:
            if not self._cooling_for(("url", url), host, now):
                return True
        return False

    def _cooling_for(self, key: tuple, host: str, now: float) -> bool:
        expiry = self._cooling.get((key, host))
        if expiry is None:
            return False
        if expiry > now:
            return True
        del self._cooling[(key, host)]
        return False

    def _key(self, proxy) -> tuple:
        """Cooldown identity: a gateway is one endpoint, a list entry one URL."""
        group = getattr(proxy, "group", None)
        if group is not None and group in self._gateways:
            return ("gateway", group)
        url = proxy if isinstance(proxy, str) else getattr(proxy, "url", None)
        return ("url", url)

    def _token_for(self, host: str, gateway: Gateway) -> str:
        if gateway.session == "sticky":
            key = (host, gateway.label)
            token = self._sticky.get(key)
            if token is None:
                token = self._token()
                self._sticky[key] = token
        else:
            token = self._token()
        _append(self._tokens, token)
        del self._tokens[:-MAX_TOKEN_SECRETS]
        return token


def _append(values: list[str], value) -> None:
    """Append a unique, non-empty string (order preserved)."""
    if value and str(value) not in values:
        values.append(str(value))


# --- config parsing -------------------------------------------------------

def _cooldown(cfg: dict) -> float:
    value = cfg.get("cooldown_sec", DEFAULT_COOLDOWN_SEC)
    if isinstance(value, bool) or value is None:
        raise ValueError(f"cooldown_sec must be a number, got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"cooldown_sec must be a number, got {value!r}") \
            from None


def _read(raw, env, base_dir):
    """(entries, gateway specs) from any accepted ``proxies`` shape."""
    entries: list[tuple[str, str | None]] = []
    gateways: list[dict] = []
    if isinstance(raw, str):
        entry = _entry(raw)
        if entry:
            entries.append(entry)
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            entry = _entry(item)
            if entry:
                entries.append(entry)
    elif isinstance(raw, dict):
        for key, value in raw.items():
            name = str(key)
            if name == "gateway":
                for spec in (value if isinstance(value, (list, tuple))
                             else [value]):
                    if not isinstance(spec, dict):
                        raise ValueError("proxies.gateway must be a mapping")
                    gateways.append(spec)
            elif name == "list" and isinstance(value, (str, os.PathLike)):
                entries.extend(_file_entries(value, base_dir))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    entry = _entry(item)
                    if entry:
                        entries.append((entry[0], entry[1] or name))
            elif isinstance(value, str):
                entries.append((value, name))
            else:
                raise ValueError(f"proxies.{name} must be a list of URLs")
    else:
        raise ValueError("proxies must be a list of URLs or a mapping")
    return entries, gateways


def _entry(item) -> tuple[str, str | None] | None:
    """One "url # label" entry -> (url, label); comments are None."""
    if not isinstance(item, str):
        raise ValueError(f"proxy entry must be a string, got {item!r}")
    text = item.strip()
    if not text or text.startswith("#"):
        return None
    match = _LABEL.search(text)
    if match is None:
        return text, None
    label = text[match.end():].strip()
    url = text[:match.start()].strip()
    if not url:
        return None
    return url, (label or None)


def _file_entries(path, base_dir) -> list[tuple[str, str | None]]:
    target = Path(path)
    if not target.is_absolute():
        target = Path(base_dir) / target
    entries = []
    for line in target.read_text(encoding="utf-8").splitlines():
        entry = _entry(line)
        if entry:
            entries.append(entry)
    return entries


def _derive_group(url: str) -> str:
    """IPv4 /16, IPv6 /48, or the hostname itself."""
    host = urlparse(url).hostname or url
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 4:
        return ".".join(str(address).split(".")[:2]) + "/16"
    return str(ipaddress.ip_network(f"{address}/48", strict=False))


def _netloc(url: str) -> str | None:
    """host:port for a proxy URL: the address without any credentials."""
    parts = urlparse(url)
    if not parts.hostname:
        return None
    return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname


def _password_of(url: str) -> str | None:
    match = _URL_PASSWORD.match(url)
    return match.group(1) if match else None


def read_proxies_file(path: str):
    """A proxies file for `run --proxies`: YAML (mapping or list) or lines.

    A plain list keeps its "# label" comments (YAML would eat them), so the
    lines are parsed here unless the file is an explicit YAML/JSON structure.
    """
    text = Path(path).read_text(encoding="utf-8")
    if _looks_like_mapping(text):
        import yaml
        parsed = yaml.safe_load(text)
        if isinstance(parsed, dict):
            return parsed
    if _looks_like_flow(text):
        import yaml
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    entries = [line.strip() for line in text.splitlines()
               if line.strip() and not line.strip().startswith("#")]
    if not entries:
        raise ValueError(f"{path}: no proxies found")
    return entries


def _looks_like_mapping(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "://" in stripped:
            return False
        return bool(re.match(r"^[A-Za-z0-9_.\-]+:\s*(\[|$)", stripped))
    return False


def _looks_like_flow(text: str) -> bool:
    return text.lstrip()[:1] in ("[", "{")
