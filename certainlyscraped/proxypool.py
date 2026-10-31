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
from urllib.parse import quote, urlparse

from .fetch import redact

DEFAULT_COOLDOWN_SEC = 300
DEFAULT_GROUP = "default"
DEFAULT_GATEWAY_GROUP = "gateway"
ENV_PROXIES = "CERTAINLYSCRAPED_PROXIES"

# Scalar tuning keys that may sit in the same mapping as the proxies
# (spec section 5). They are read by the pool and by the ladder; a group
# cannot be named after one.
TUNING_KEYS = ("per_proxy_rate_sec", "cooldown_sec", "max_retries")

# Where a password is allowed to live (R31): a proxies file or the
# environment. Never the main config, which is the file people commit.
PASSWORD_HINT = ("move it to a proxies file passed with --proxies, or use a "
                 "gateway template with password_env")

# Outcomes that park a (proxy, host) pair. Anything else (ok, not_found) is a
# vote of confidence and clears the cooldown.
COOLING_OUTCOMES = frozenset({"blocked", "rate_limited", "error"})

SESSION_BYTES = 4  # token_hex(4) -> 8 hex chars
# Rotate tokens kept for redaction: enough for anything still in flight.
MAX_TOKEN_SECRETS = 512

# "url # label" / "url #label": the label is the group name.
_LABEL = re.compile(r"\s+#\s*")
# The password runs to the LAST '@' in the authority: a password may itself
# contain one ('http://u:p@ss@h:1' has the password 'p@ss').
_URL_PASSWORD = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/:@]*:(.*)@")

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
        _reject_template_password(template)
        env_name = cfg.get("password_env")
        if not isinstance(env_name, str) or not env_name:
            raise ValueError("gateway: password_env is required (the name of "
                             "the environment variable holding the password)")
        if env_name not in env:
            raise ValueError(f"gateway: environment variable {env_name} is "
                             "not set")
        self.password = str(env[env_name])
        # The URL is built with percent-encoded credentials, so a password
        # containing '@', ':' or '/' cannot split the userinfo (R32b).
        self.encoded_password = quote(self.password, safe="")
        self.host = None if cfg.get("host") is None else str(cfg["host"])
        self.port = None if cfg.get("port") is None else str(cfg["port"])
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
        for key in ("user", "password", "pass", "session"):
            if key in values:
                values[key] = quote(values[key], safe="")
        values["session"] = quote(token, safe="")
        values["pass"] = values["password"] = self.encoded_password
        if "user" not in values:
            values["user"] = ""
        return self.template.format(**values)


def _reject_template_password(template: str) -> None:
    """A gateway template is main-config text: no literal password in it (R31).

    ``{password}`` (or ``{pass}``) is the only way a credential gets into the
    built URL, and it comes from ``password_env``.
    """
    authority = template.split("://", 1)[-1].split("/", 1)[0]
    if "@" not in authority:
        return
    userinfo = authority.rpartition("@")[0]
    if ":" not in userinfo:
        return
    literal = re.sub(r"\{[^}]*\}", "", userinfo.split(":", 1)[1]).strip()
    if literal:
        raise ValueError("gateway: a literal password in the template is "
                         "refused (the config is usually committed): use the "
                         "{password} placeholder with password_env, or " +
                         PASSWORD_HINT)


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
        # Every address the pool itself knows: the URL, host:port and the
        # bare host. netguard says "private address: 10.1.2.3", a client
        # quotes the URL, and a sandbox error says "gw.example" (R32a).
        self._addresses: list[str] = []
        for label, urls in groups.items():
            urls = [str(url) for url in urls]
            if not urls:
                continue
            self._groups[str(label)] = urls
            for url in urls:
                _append(self._passwords, _password_of(url))
                _append(self._addresses, url)
                _append(self._addresses, _netloc(url))
                _append_host(self._addresses, _hostname(url))
        for gateway in gateways:
            if gateway.label in self._groups or gateway.label in self._gateways:
                raise ValueError("proxy group label is used twice: "
                                 f"{gateway.label!r}")
            self._gateways[gateway.label] = gateway
            _append(self._passwords, gateway.password)
            _append(self._passwords, gateway.encoded_password)
            # The spec's own s5 example names the gateway host inside the
            # template and nowhere else, so the address to scrub has to come
            # out of the URL the template builds (R33/misc): host,
            # host:port and the URL itself.
            sample = gateway.build("session")
            _append(self._addresses, sample)
            _append(self._addresses, _netloc(sample))
            _append_host(self._addresses, _hostname(sample))
            if gateway.host:
                _append_host(self._addresses, gateway.host)
                if gateway.port:
                    _append(self._addresses, f"{gateway.host}:{gateway.port}")
        self._order = list(self._groups) + list(self._gateways)
        self._turn = {label: 0 for label in self._order}
        self._rr = 0
        self._cooling: dict[tuple, float] = {}
        self._sticky: dict[tuple, str] = {}

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict, env=None, base_dir: str = ".",
                    *, allow_userinfo: bool = False, use_env: bool = True,
                    proxy_source: str = "auto",
                    clock=time.monotonic, token_factory=None):
        """The pool a config asks for, or None when it names no proxy.

        Precedence (R35): ``--no-proxy`` > an explicit ``--proxies FILE`` >
        ``CERTAINLYSCRAPED_PROXIES`` > the config. ``use_env=False`` is the
        first rung (nothing comes through the environment); ``proxy_source=
        "flag"`` is the second: the caller already put the flag's list in
        ``cfg["proxies"]``, so the environment must not override it.

        ``allow_userinfo`` says the proxy list came from a dedicated proxies
        file (or the environment), where a password is allowed. A password
        inline in the main config is refused (R31).
        """
        cfg = cfg or {}
        env = os.environ if env is None else env
        if proxy_source not in ("auto", "flag"):
            raise ValueError("proxy_source must be 'auto' or 'flag', got "
                             f"{proxy_source!r}")
        raw = cfg.get("proxies")
        override = ((env or {}).get(ENV_PROXIES)
                    if (env is not None and use_env
                        and proxy_source != "flag") else None)
        if override:
            raw = [part.strip() for part in re.split(r"[,\n]", override)
                   if part.strip()]
            allow_userinfo = True  # the environment is a dedicated channel
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
        tuning = _tuning(raw, cfg)
        entries, gateways = _read(raw, env, base_dir,
                                  allow_userinfo=allow_userinfo)
        groups: dict[str, list[str]] = {}
        for url, label in entries:
            group = label or default_group or _derive_group(url)
            groups.setdefault(group, []).append(url)
        built = [Gateway(spec, env) for spec in gateways]
        if not groups and not built:
            return None
        return cls(groups, built,
                   cooldown_sec=_cooldown(tuning.get("cooldown_sec")),
                   clock=clock, token_factory=token_factory)

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
            targets = self._passwords + self._tokens + self._addresses
        return redact(text, targets)

    # -- handing out -------------------------------------------------------

    def acquire(self, host: str) -> Proxy | None:
        with self._lock:
            return self._issue(host, frozenset())

    def alternate(self, host: str, exclude_groups) -> Proxy | None:
        with self._lock:
            return self._issue(host, frozenset(exclude_groups or ()))

    def report(self, proxy, host: str, outcome: str, *,
               challenge: bool = False) -> None:
        """Rate-limited/plain-blocked/error park the pair; ok clears it.

        R34: a challenge parks nothing and clears nothing. The page came
        back walled *with* challenge markers, which means the proxy reached
        the site -- the site is the problem, so penalising the endpoint
        would only send the next page onto a proxy that is no better and
        burn a good one out of the rotation.
        """
        if proxy is None:
            return
        if outcome == "blocked" and challenge:
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
            # Spec s5: one session per (host, worker) -- a worker keeps its
            # identity across pages, two workers never share one (R30).
            key = (host, threading.get_ident())
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


def _append_host(values: list[str], host) -> None:
    """Scrub a bare hostname, unless it is too short to be told from a word.

    ``10.1.2.3`` and ``gw.example`` are always masked; a single-label name is
    masked from four characters up. Anything shorter would replace ordinary
    prose ("a", "gw") all over a log, and ``host:port`` plus the URL still
    name the proxy unambiguously.
    """
    if not host:
        return
    host = str(host)
    if len(host) >= 4 or "." in host or ":" in host:
        _append(values, host)


# --- config parsing -------------------------------------------------------

def _tuning(raw, cfg: dict) -> dict:
    """The scalar tuning keys: from the ``proxies`` block, else the top level."""
    block = raw if isinstance(raw, dict) else {}
    return {key: block.get(key, cfg.get(key)) for key in TUNING_KEYS}


def _cooldown(value) -> float:
    if value is None:
        return DEFAULT_COOLDOWN_SEC
    if isinstance(value, bool):
        raise ValueError(f"cooldown_sec must be a number, got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"cooldown_sec must be a number, got {value!r}") \
            from None


def _read(raw, env, base_dir, *, allow_userinfo: bool = False):
    """(entries, gateway specs) from any accepted ``proxies`` shape."""
    entries: list[tuple[str, str | None]] = []
    gateways: list[dict] = []

    def add(item, label=None):
        entry = _entry(item, allow_userinfo, default_label=label)
        if entry:
            entries.append(entry)

    if isinstance(raw, str):
        add(raw)
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            add(item)
    elif isinstance(raw, dict):
        for key, value in raw.items():
            name = str(key)
            if name in TUNING_KEYS:
                continue
            if name == "gateway":
                for spec in (value if isinstance(value, (list, tuple))
                             else [value]):
                    if not isinstance(spec, dict):
                        raise ValueError(
                            "proxies.gateway is reserved for a gateway "
                            "mapping; a group cannot be named 'gateway'")
                    gateways.append(spec)
            elif name == "list":
                if not isinstance(value, (str, os.PathLike)):
                    raise ValueError(
                        "proxies.list is reserved for a file path; a group "
                        "cannot be named 'list' (label a list entry instead)")
                entries.extend(_file_entries(value, base_dir))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    add(item, name)
            elif isinstance(value, str):
                add(value, name)
            else:
                raise ValueError(f"proxies.{name} must be a list of URLs")
    else:
        raise ValueError("proxies must be a list of URLs or a mapping")
    return entries, gateways


def _entry(item, allow_userinfo: bool = False,
           default_label: str | None = None):
    """One "url # label" entry -> (url, label); comments are None."""
    if not isinstance(item, str):
        raise ValueError(f"proxy entry must be a string, got {item!r}")
    text = _unbullet(item.strip())
    if not text or text.startswith("#"):
        return None
    match = _LABEL.search(text)
    if match is None:
        url, label = text, None
    else:
        label = text[match.end():].strip() or None
        url = text[:match.start()].strip()
    if not url:
        return None
    if not allow_userinfo:
        _reject_password(url)
    return url, (label or default_label)


def _reject_password(url: str) -> None:
    """A password belongs in a proxies file, never in the main config (R31)."""
    if _password_of(url) is None:
        return
    raise ValueError(
        "a password in a proxy URL is refused in the main config (the file "
        "is usually committed): " + PASSWORD_HINT)


def _unbullet(text: str) -> str:
    """Strip the '- ' of a YAML block-list item."""
    return re.sub(r"^-[ \t]+", "", text).strip()


def _file_entries(path, base_dir) -> list[tuple[str, str | None]]:
    target = Path(path)
    if not target.is_absolute():
        target = Path(base_dir) / target
    entries = []
    for number, line in enumerate(target.read_text(encoding="utf-8")
                                  .splitlines(), 1):
        entry = _entry(line, allow_userinfo=True)
        if not entry:
            continue
        if not _looks_like_proxy_url(entry[0]):
            raise ValueError(f"{target}: line {number}: not a proxy URL "
                             "(needs scheme://host:port)")
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


def _hostname(url: str) -> str | None:
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _netloc(url: str) -> str | None:
    """host:port for a proxy URL: the address without any credentials."""
    try:
        parts = urlparse(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    return f"{host}:{port}" if port else host


def _looks_like_proxy_url(text: str) -> bool:
    """True for a ``scheme://host`` proxy target (a password is allowed)."""
    if "://" not in text:
        return False
    try:
        parts = urlparse(text)
        return bool(parts.scheme and parts.hostname)
    except ValueError:
        return False


def _password_of(url: str) -> str | None:
    match = _URL_PASSWORD.match(url)
    return match.group(1) if match else None


def read_proxies_file(path: str):
    """A proxies file for `run --proxies`: YAML (mapping or list) or lines.

    A plain list keeps its "# label" comments (YAML would eat them), so the
    lines are parsed here unless the file is an explicit YAML/JSON structure.
    A line that is not a proxy URL is refused at load time, with its number.
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
        if isinstance(parsed, dict):
            return parsed
    entries = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = _unbullet(line.strip())
        if not stripped or stripped.startswith("#"):
            continue
        entry = _entry(stripped, allow_userinfo=True)
        if entry is None:
            continue
        if not _looks_like_proxy_url(entry[0]):
            raise ValueError(f"{path}: line {number}: not a proxy URL "
                             "(needs scheme://host:port)")
        url, label = entry
        entries.append(f"{url} # {label}" if label else url)
    if not entries:
        raise ValueError(f"{path}: no proxies found")
    return entries


def _looks_like_mapping(text: str) -> bool:
    """True when the first meaningful line is 'name: ...' (a YAML mapping).

    A URL line ('http://p1:1') is not one: its scheme is followed by '//',
    not by whitespace or the end of the line -- and neither is a block-list
    item ('- http://p1:1').
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            return False
        return bool(re.match(r"^[A-Za-z0-9_.\-]+:(\s|$)", stripped))
    return False


def _looks_like_flow(text: str) -> bool:
    return text.lstrip()[:1] in ("[", "{")
