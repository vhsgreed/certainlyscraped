"""The verdict column is the claim people read; it must not flatter the tool.

Two false `rows` were found by hand on 2026-11-01: chrono24's "10 items" were
analytics pushes in an embedded JSON blob, and super-hobby's were a homepage
carousel with no prices. Each test below is one of those mistakes.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("coverage_script", ROOT / "scripts" / "coverage.py")
assert _spec and _spec.loader
cov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cov)


def _r(group="tech", items="30", fields=("title", "url"), source="html", walled=False):
    return {"group": group, "items": items, "fields": list(fields),
            "source": source, "walled": walled}


def test_a_plain_html_list_is_rows():
    assert cov.verdict(_r()) == "rows"


def test_analytics_json_is_not_rows():
    # chrono24: dataLayerPushes, fields "0", "1", "2_ad_storage"...
    r = _r(group="marketplaces", items="10", source="json",
           fields=("0", "1", "2_ad_storage", "page_c24sid"))
    assert cov.verdict(r) == "noise"


def test_a_consent_dialog_in_embedded_json_is_noise():
    # youtube-search: 83 "items" were the cookie-consent language dropdown,
    # one key each. The path gives it away even when the keys do not.
    r = _r(group="social", items="83", source="json",
           fields=("dropdownitemrenderer_stringvalue",))
    r["json_path"] = ("topbar.desktopTopbarRenderer.interstitial."
                      "consentBumpV2Renderer.languageList.dropdownRenderer.entries")
    assert cov.verdict(r) == "noise"


def test_embedded_json_with_content_keys_is_rows():
    r = _r(items="10", source="json", fields=("text", "author", "tags"))
    r["json_path"] = "data.quotes"
    assert cov.verdict(r) == "rows"


def test_a_marketplace_list_without_prices_is_not_rows():
    # super-hobby homepage: 10 product tiles, title/url/image, no price.
    r = _r(group="marketplaces", items="10", fields=("title", "url", "image"))
    assert cov.verdict(r) == "no-price"


def test_a_marketplace_list_with_prices_is_rows():
    r = _r(group="marketplaces", items="54", fields=("title", "url", "image", "price"))
    assert cov.verdict(r) == "rows"


def test_non_marketplaces_do_not_need_a_price():
    # Hacker News and Wikipedia have no prices and are real lists.
    assert cov.verdict(_r(group="tech", fields=("title", "url", "rank"))) == "rows"


def test_existing_verdicts_still_hold():
    assert cov.verdict(_r(walled=True)) == "walled"
    assert cov.verdict(_r(items="0", fields=())) == "shell"
    assert cov.verdict(_r(items="3")) == "thin"
    assert cov.verdict(_r(items="-")) == "error"


def test_no_price_counts_against_check_only_for_sandbox():
    # The sandbox is the control group; a no-price there means the price
    # detector broke, and --check should say so.
    assert "no-price" not in cov.PASSING
    assert "rows" in cov.PASSING
