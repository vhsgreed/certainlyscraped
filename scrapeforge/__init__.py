"""Deprecated alias for `certainlyscraped`; removed in 0.5.0.

`import scrapeforge` keeps working for one release: it re-exports the public
API and registers every `certainlyscraped` submodule under the old name so
`import scrapeforge.fetch` and friends resolve to the same module objects.
"""
import importlib
import pkgutil
import sys
import warnings

import certainlyscraped
from certainlyscraped import *  # noqa: F401,F403

warnings.warn(
    "scrapeforge was renamed to certainlyscraped; the old name goes away "
    "in 0.5.0",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = list(certainlyscraped.__all__)

_info = _module = None
for _info in pkgutil.iter_modules(certainlyscraped.__path__):
    # `__main__` must stay out of sys.modules: aliasing it makes runpy
    # resolve `scrapeforge.__main__` to an unloadable module and `python -m
    # scrapeforge` dies before our own __main__.py can run.
    if _info.name == "__main__":
        continue
    try:
        _module = importlib.import_module(f"certainlyscraped.{_info.name}")
    except ModuleNotFoundError as _e:
        # A module that needs an optional extra (`mcp_server` needs
        # `certainlyscraped[mcp]`) is skipped, as `import certainlyscraped`
        # would never load it either. Anything missing from our own package
        # is a real error and still raises.
        if (_e.name or "").split(".")[0] in ("certainlyscraped", "scrapeforge"):
            raise
        continue
    sys.modules[f"{__name__}.{_info.name}"] = _module
    globals()[_info.name] = _module

del _info, _module


# The console entry point lives in certainlyscraped.cli so it does not have to
# import this package (whose import warns). Kept exported for `python -m` and
# for callers of the old attribute.
from certainlyscraped.cli import _deprecated_main  # noqa: E402,F401
