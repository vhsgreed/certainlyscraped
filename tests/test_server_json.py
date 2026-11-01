"""server.json is the MCP Registry entry; it must name the release it ships with."""
import json
import re
from pathlib import Path

import certainlyscraped

ROOT = Path(__file__).resolve().parent.parent


def test_server_json_matches_the_package_version():
    entry = json.loads((ROOT / "server.json").read_text())
    v = certainlyscraped.__version__
    assert entry["version"] == v
    (pkg,) = entry["packages"]
    assert pkg["identifier"] == "certainlyscraped" and pkg["version"] == v
    assert pkg["runtimeArguments"][0]["value"] == f"certainlyscraped[mcp]=={v}"
    assert len(entry["description"]) <= 100


def test_readme_carries_the_registry_ownership_marker():
    entry = json.loads((ROOT / "server.json").read_text())
    readme = (ROOT / "README.md").read_text()
    assert re.search(rf"mcp-name: {re.escape(entry['name'])}(\s|-->)", readme)
