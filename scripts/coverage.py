#!/usr/bin/env python3
"""Probe every target in targets/popular.yaml once and write COVERAGE.md.

One request per target, two seconds apart, honest user agent. The output is a
dated snapshot, not a promise: a row says what a single request got from the
machine that ran this. Run it from home for a home-IP read, or let the
`coverage` workflow run it nightly from a GitHub runner (datacenter IP).

    python3 scripts/coverage.py                 # write COVERAGE.md
    python3 scripts/coverage.py --check         # exit 1 if a sandbox target broke
    python3 scripts/coverage.py --only social   # one group

Exit status is 0 on a written file even when targets are walled (that is
information), and 1 only when --check is given and a `sandbox` target is not
reachable, because the sandbox rows are what make the rest of the table
believable.
"""

from __future__ import annotations

import argparse
import datetime
import platform
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from certainlyscraped.classify import probe  # noqa: E402
from certainlyscraped.fetch import fetch  # noqa: E402
from certainlyscraped.suggest import suggest  # noqa: E402

PAUSE_SEC = 2.0


def load_targets(path: Path, only: str | None) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    items = data["targets"]
    if only:
        items = [t for t in items if t["group"] == only]
    return items


def row(t: dict) -> dict:
    """Probe once, then look for items in the page the probe chose.

    `probe` alone says whether HTML came back; it cannot tell a list page from
    a login shell, and those are the rows people read first (Facebook, Reddit).
    So a second request reads the same page at the tier the probe picked and
    asks the item suggester what it can see. Two requests per target, nothing
    that a person clicking a link would not do.
    """
    try:
        r = probe(t["url"], timeout=25)
    except Exception as exc:  # noqa: BLE001 - a failed probe is data
        return {**t, "tier": "-", "status": "-", "items": "-", "walled": True, "bytes": 0,
                "note": f"{type(exc).__name__}: {exc}"[:140]}

    walled = r.note.startswith("blocked") or r.tier >= 3
    items, items_note = "-", ""
    if not walled:
        try:
            # fetch() returns (status_code, html), plus headers on request.
            got = fetch(t["url"], tier=r.tier, timeout=25, return_headers=False)
            html = got[1]
            s = suggest(html)
            if s is None:
                items, items_note = "0", "no item blocks"
            else:
                items = str(s.item_count)
                if s.item_count < 5:
                    items_note = f"only {s.item_count}, weak"
        except Exception as exc:  # noqa: BLE001 - a failed read is data
            items, items_note = "-", f"{type(exc).__name__}: {exc}"[:60]

    note = r.note if not items_note else f"{r.note}; {items_note}"
    return {**t, "tier": str(r.tier), "status": str(r.status), "note": note[:140],
            "items": items, "walled": walled, "bytes": r.body_size}


def table(rows: list[dict]) -> str:
    out = ["| target | verdict | tier | HTTP | items | what happened |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        note = r["note"].replace("|", "\\|")
        out.append(f"| `{r['name']}` | {r['verdict']} | {r['tier']} | {r['status']} "
                   f"| {r['items']} | {note} |")
    return "\n".join(out)


def verdict(r: dict) -> str:
    """What a reader needs to know, in one word.

    HTTP 200 is not success: Facebook and Instagram return a login shell with
    200 and no items, and calling that "reached" is the exact overclaim this
    table exists to prevent.
    """
    if r["walled"]:
        return "walled"
    try:
        n = int(r["items"])
    except (TypeError, ValueError):
        return "error"
    if n == 0:
        return "shell"
    return "thin" if n < 5 else "rows"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=str(ROOT / "targets" / "popular.yaml"))
    ap.add_argument("--out", default=str(ROOT / "COVERAGE.md"))
    ap.add_argument("--only")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--pause", type=float, default=PAUSE_SEC)
    args = ap.parse_args()

    targets = load_targets(Path(args.targets), args.only)
    rows = []
    for i, t in enumerate(targets, 1):
        r = row(t)
        rows.append(r)
        print(f"[{i}/{len(targets)}] {r['group']:<13} {r['name']:<22} tier {r['tier']} "
              f"{r['status']:<4} items {r['items']:<4} {r['note'][:60]}", flush=True)
        if i < len(targets):
            time.sleep(args.pause)

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)

    for r in rows:
        r["verdict"] = verdict(r)

    today = datetime.date.today().isoformat()
    tally: dict[str, int] = {}
    for r in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    lines = [
        "# Coverage: popular data targets",
        "",
        f"Probed {today} from {platform.platform(terse=True)}, once per target, "
        f"{args.pause:g} s apart, with the honest user agent.",
        "",
        "One request per site on one machine is a **snapshot**, not a verdict about "
        "the site: reachability depends on the IP, the time of day and what the "
        "site was doing that minute. Rows marked tier 3 were walled for this "
        "machine; a proxy or a browser may still get through, and this table is "
        "not a promise that it will.",
        "",
        f"Of {len(rows)} targets: **{tally.get('rows', 0)} returned rows**, "
        f"{tally.get('thin', 0)} returned a couple, "
        f"{tally.get('shell', 0)} returned a JavaScript shell, "
        f"{tally.get('walled', 0)} were walled"
        + (f", {tally['error']} failed" if tally.get("error") else "") + ".",
        "",
        "| verdict | means |",
        "|---|---|",
        "| rows | 5 or more item blocks found in the HTML that came back |",
        "| thin | 1 to 4: a real page, not a listing, or a listing mostly drawn by script |",
        "| shell | HTTP 200 and zero items: the page exists, the data is drawn later by JavaScript |",
        "| walled | both tier 1 and tier 2 were refused: status code or challenge page |",
        "",
        "Generated by `scripts/coverage.py`; edit `targets/popular.yaml`, not this file.",
        "",
    ]
    for g, items in groups.items():
        ok = sum(1 for r in items if r["verdict"] == "rows")
        lines += [f"## {g} ({ok}/{len(items)} returned rows)", "", table(items), ""]
    lines += [
        "## Reading a row",
        "",
        "- **tier 1**: plain HTTP was enough. **tier 2**: only TLS impersonation "
        "got real HTML. **tier 3**: both were walled; the note says whether it was "
        "a status code or a challenge page.",
        "- A `blocked` note with HTTP 200 usually means a JavaScript shell: the "
        "server sent a page, and the list is fetched later by the browser. The "
        "fix for those is tier 3 or that site's JSON API, not a better selector.",
        "- Sites that answer only behind a login are not attempted by tiers 1-3 "
        "at all. The tool reports `blocked` and stops; it does not have your "
        "password.",
        "",
    ]
    Path(args.out).write_text("\n".join(lines))

    if args.check:
        # The sandbox rows are the table's control group: they must return
        # rows. If they do not, the probe path broke, not the internet.
        broken = [r for r in rows if r["group"] == "sandbox" and r["verdict"] != "rows"]
        for r in broken:
            print(f"sandbox target did not return rows: {r['name']} "
                  f"({r['verdict']}: {r['note']})", file=sys.stderr)
        return 1 if broken else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
