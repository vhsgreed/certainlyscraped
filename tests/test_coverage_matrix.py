"""targets/popular.yaml drives the nightly matrix; a broken list breaks it silently."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ROOT / "targets" / "popular.yaml"


def _targets():
    return yaml.safe_load(TARGETS.read_text())["targets"]


def test_every_target_has_a_group_name_and_https_url():
    for t in _targets():
        assert set(t) == {"group", "name", "url"}, t
        assert t["url"].startswith("https://"), t
        assert t["group"] == t["group"].lower()


def test_names_are_unique_because_they_key_the_matrix():
    names = [t["name"] for t in _targets()]
    assert len(names) == len(set(names))
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes


def test_the_list_keeps_its_breadth():
    groups = {t["group"] for t in _targets()}
    # Dropping a whole category should be a decision, not a side effect.
    assert {"marketplaces", "social", "media", "tech", "sandbox"} <= groups


def test_sandboxes_are_present_because_they_prove_the_probe_still_works():
    sandboxes = [t for t in _targets() if t["group"] == "sandbox"]
    assert len(sandboxes) >= 3
