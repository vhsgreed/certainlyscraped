"""The old name keeps working for one release: import shim + command notice."""


def test_old_import_warns_and_aliases():
    import importlib, sys, warnings
    sys.modules.pop("scrapeforge", None)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        sf = importlib.import_module("scrapeforge")
    assert any(issubclass(x.category, DeprecationWarning) for x in w)
    import certainlyscraped.paginate
    assert importlib.import_module("scrapeforge.paginate") is certainlyscraped.paginate
    assert sf.scrape is certainlyscraped.scrape


def test_old_command_prints_notice(capsys):
    import scrapeforge
    try:
        scrapeforge._deprecated_main(["probe", "--help"])
    except SystemExit:
        pass
    assert "renamed to certainlyscraped" in capsys.readouterr().err
