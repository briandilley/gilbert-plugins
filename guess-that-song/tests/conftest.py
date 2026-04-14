"""Configure the plugin package for test imports.

The guess-that-song plugin uses relative imports (``from .game import ...``),
so pytest needs to see the plugin directory as a proper Python package.
This conftest registers it once before any test collection happens.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "guess_that_song"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    for _mod_name in ("game", "scoring", "service"):
        # NOTE: do NOT pass ``submodule_search_locations`` here. Passing
        # ``[]`` would flag the module as a package whose ``__path__`` is
        # empty, which makes its ``__package__`` be
        # ``guess_that_song.<module>`` and causes intra-plugin relative
        # imports like ``from .game import ...`` inside ``service.py``
        # to resolve to a *second* copy of the module at
        # ``guess_that_song.service.game``. Omitting the kwarg lets
        # Python treat these as plain submodules whose ``__package__``
        # is ``guess_that_song`` — see the unifi conftest and the
        # ``memory-unifi-relative-imports`` memory file for the gory
        # details.
        _spec = importlib.util.spec_from_file_location(
            f"{_pkg_name}.{_mod_name}",
            _plugin_dir / f"{_mod_name}.py",
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_pkg_name}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(pkg, _mod_name, _mod)
