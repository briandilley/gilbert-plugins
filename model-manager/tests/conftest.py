"""Register the model-manager plugin as a Python package for tests.

The plugin uses relative imports (``from . import hf_catalog`` in
``model_manager_service.py``, ``from .recommended import …`` in
``hf_catalog.py``, ``from .model_manager_service import …`` in
``plugin.py``), so pytest needs to see the plugin directory as a proper
Python package. Mirrors ``std-plugins/unifi/tests/conftest.py`` — see its
comment for why ``submodule_search_locations`` must NOT be passed: doing so
makes each module a package whose intra-plugin ``from .x import …`` resolves
to a *second* copy of ``x`` (distinct objects → monkeypatch misses, the
service calls the original while the test patches the duplicate).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_model_manager"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    # Leaf modules first, then dependents (recommended ← hf_catalog, fit ←
    # model_manager_service ← plugin), so each relative import binds to the
    # already-registered single copy.
    for _mod_name in ("recommended", "hf_catalog", "fit", "model_manager_service", "plugin"):
        _spec = importlib.util.spec_from_file_location(
            f"{_pkg_name}.{_mod_name}",
            _plugin_dir / f"{_mod_name}.py",
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_pkg_name}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(pkg, _mod_name, _mod)
