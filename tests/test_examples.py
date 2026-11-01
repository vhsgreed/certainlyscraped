"""Every shipped example must load and be runnable as written.

An example config that no longer parses is worse than no example: it is the
first thing a new user runs. These checks are offline; `live.yml` is what
fetches the real pages.
"""
from pathlib import Path

import yaml

from certainlyscraped.api import load_config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "examples").glob("*.yaml"))


def test_there_are_examples_to_check():
    assert len(EXAMPLES) >= 5


def test_every_example_loads_through_the_real_loader():
    for path in EXAMPLES:
        cfg = load_config(str(path))          # raises on a bad config
        items = cfg["items"]
        # Most examples select items; an embedded-JSON config names the path
        # into the page's data instead.
        assert items.get("selector") or items.get("json"), path.name
        assert cfg["fields"], path.name
        assert str(cfg["url"]).startswith(("http://", "https://")), path.name


def test_every_example_name_is_unique_and_matches_its_file():
    names = [load_config(str(p))["name"] for p in EXAMPLES]
    assert len(names) == len(set(names))
    for path in EXAMPLES:
        assert path.stem and " " not in path.stem


def test_pagination_blocks_are_well_formed():
    for path in EXAMPLES:
        pag = yaml.safe_load(path.read_text()).get("paginate")
        if not pag:
            continue
        # A paginate block either follows a link or fills a page template.
        assert bool(pag.get("selector")) != bool(pag.get("url")), path.name
        if pag.get("url"):
            assert "{page}" in pag["url"], f"{path.name}: template without {{page}}"
        assert load_config(str(path))["max_pages"] <= 10, path.name
