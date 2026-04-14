"""Register the unifi plugin as a Python package for tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_unifi"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    # Order matters — leaf modules first, then dependents.
    for _mod_name in (
        "client",
        "name_resolver",
        "access",
        "network",
        "protect",
        "presence",
        "doorbell",
        "plugin",
    ):
        # NOTE: do NOT pass ``submodule_search_locations`` here. Passing
        # ``[]`` would flag the module as a package whose ``__path__`` is
        # empty, which in turn makes its ``__package__`` be
        # ``gilbert_plugin_unifi.<module>`` and causes intra-plugin
        # relative imports like ``from .client import ...`` inside
        # ``presence.py`` to resolve to a *second* copy of the module at
        # ``gilbert_plugin_unifi.presence.client``. Omitting the kwarg
        # lets Python treat these as plain submodules whose
        # ``__package__`` is ``gilbert_plugin_unifi``, which is what we
        # want.
        _spec = importlib.util.spec_from_file_location(
            f"{_pkg_name}.{_mod_name}",
            _plugin_dir / f"{_mod_name}.py",
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_pkg_name}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(pkg, _mod_name, _mod)
