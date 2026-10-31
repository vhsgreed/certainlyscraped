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


def _without_mcp(code):
    """Run `code` in a fresh interpreter where the optional `mcp` extra is absent."""
    import subprocess, sys

    prelude = (
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'mcp' or name.startswith('mcp.'):\n"
        "            raise ModuleNotFoundError(f\"No module named '{name}'\", name=name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
    )
    return subprocess.run(
        [sys.executable, "-c", prelude + code], capture_output=True, text=True
    )


def test_old_import_works_without_the_mcp_extra():
    r = _without_mcp(
        "import warnings; warnings.simplefilter('ignore')\n"
        "import scrapeforge, certainlyscraped.fetch\n"
        "assert scrapeforge.fetch is certainlyscraped.fetch\n"
        "assert 'scrapeforge.mcp_server' not in sys.modules\n"
        "print('ok')\n"
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def test_python_m_scrapeforge_runs_without_the_mcp_extra():
    r = _without_mcp(
        "import runpy; sys.argv = ['scrapeforge', '--help']\n"
        "runpy.run_module('scrapeforge', run_name='__main__')\n"
    )
    assert r.returncode == 0, r.stderr
    assert "renamed to certainlyscraped" in r.stderr


def test_a_real_import_error_inside_the_package_still_raises():
    # Only a missing *optional third-party* dependency is skipped; a broken
    # certainlyscraped module must not be hidden by the shim.
    r = _without_mcp(
        "import certainlyscraped, types\n"
        "sys.modules['certainlyscraped.fetch'] = None\n"
        "try:\n"
        "    import scrapeforge\n"
        "except ImportError:\n"
        "    print('raised')\n"
    )
    assert r.stdout.strip() == "raised", (r.stdout, r.stderr)
