#!/usr/bin/env python3
"""Set the version in every place that carries one.

Two files must agree or the release is wrong: the package version and
`server.json` (the MCP Registry entry pins the PyPI spec it starts). The
release workflow refuses a tag that does not match the package, and
`tests/test_server_json.py` refuses a `server.json` that does not match the
package, so an incomplete bump fails in CI after the tag exists. This is the
one command that does all of it.

    python3 scripts/bump_version.py 0.4.5
    python3 scripts/bump_version.py 0.4.5 --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "certainlyscraped" / "__init__.py"
SERVER = ROOT / "server.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        print(f"not a version: {args.version!r} (want MAJOR.MINOR.PATCH)", file=sys.stderr)
        return 2

    init_text = INIT.read_text()
    m = re.search(r'__version__ = "(.+?)"', init_text)
    if not m:
        print("no __version__ in certainlyscraped/__init__.py", file=sys.stderr)
        return 1
    old = m.group(1)

    new_init = init_text.replace(f'__version__ = "{old}"', f'__version__ = "{args.version}"', 1)

    entry = json.loads(SERVER.read_text())
    entry["version"] = args.version
    for pkg in entry.get("packages", []):
        pkg["version"] = args.version
        for a in pkg.get("runtimeArguments", []):
            if isinstance(a.get("value"), str) and "certainlyscraped[mcp]==" in a["value"]:
                a["value"] = f"certainlyscraped[mcp]=={args.version}"
    new_server = json.dumps(entry, indent=2) + "\n"

    print(f"{old} -> {args.version}")
    for path, before, after in ((INIT, init_text, new_init), (SERVER, SERVER.read_text(), new_server)):
        print(f"  {'would write' if args.dry_run else 'wrote'} {path.relative_to(ROOT)}"
              f"{'' if before != after else ' (no change)'}")
    if not args.dry_run:
        INIT.write_text(new_init)
        SERVER.write_text(new_server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
