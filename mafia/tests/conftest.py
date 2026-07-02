"""Register the mafia plugin as a Python package for tests.

The plugin uses relative imports (``from .service import MafiaService`` in
``plugin.py``), so pytest needs to see the plugin directory as a proper
Python package. Mirrors ``std-plugins/model-manager/tests/conftest.py`` — do NOT
pass `submodule_search_locations=[]` to spec_from_file_location, as doing so
marks each module as a package whose intra-plugin ``from .x import …`` resolves
to a *second* copy of ``x`` (distinct objects → monkeypatch misses, the
service calls the original while the test patches the duplicate).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_mafia"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    # Leaf modules first, then dependents (service ← plugin), so each
    # relative import binds to the already-registered single copy.
    # Note: game and narrator modules will be added in later tasks.
    for _mod_name in ("service", "plugin"):
        _spec = importlib.util.spec_from_file_location(
            f"{_pkg_name}.{_mod_name}",
            _plugin_dir / f"{_mod_name}.py",
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_pkg_name}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(pkg, _mod_name, _mod)
