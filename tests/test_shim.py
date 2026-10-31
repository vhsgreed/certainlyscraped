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


def test_attribute_path_aliases():
    import scrapeforge
    import certainlyscraped.fetch

    assert scrapeforge.fetch is certainlyscraped.fetch


def test_python_m_scrapeforge_runs():
    import subprocess, sys

    result = subprocess.run(
        [sys.executable, "-m", "scrapeforge", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "renamed to certainlyscraped" in result.stderr
    assert result.stderr.count("renamed to certainlyscraped") == 1
    assert "RuntimeWarning" not in result.stderr
    assert "DeprecationWarning" not in result.stderr


def test_old_command_entry_point_is_the_new_module():
    # The console script must not import the shim (which warns): one notice.
    import scrapeforge
    from certainlyscraped.cli import _deprecated_main

    assert scrapeforge._deprecated_main is _deprecated_main


def test_old_command_prints_notice(capsys):
    import scrapeforge
    try:
        scrapeforge._deprecated_main(["probe", "--help"])
    except SystemExit:
        pass
    assert "renamed to certainlyscraped" in capsys.readouterr().err
