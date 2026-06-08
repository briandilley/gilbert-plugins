"""Register the ollama plugin as a Python package for tests."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

_plugin_dir = Path(__file__).resolve().parent.parent
_pkg_name = "gilbert_plugin_ollama"

if _pkg_name not in sys.modules:
    pkg = ModuleType(_pkg_name)
    pkg.__path__ = [str(_plugin_dir)]
    pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = pkg

    # ``_installed_cache`` is a leaf shared by both ``ollama_ai`` and
    # ``ollama_runtime`` (each does ``from . import _installed_cache``); load
    # it first so those relative imports bind to the single registered copy.
    for _mod_name in ("_installed_cache", "ollama_ai", "ollama_runtime", "plugin"):
        _spec = importlib.util.spec_from_file_location(
            f"{_pkg_name}.{_mod_name}",
            _plugin_dir / f"{_mod_name}.py",
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[f"{_pkg_name}.{_mod_name}"] = _mod
        _spec.loader.exec_module(_mod)
        setattr(pkg, _mod_name, _mod)


@pytest.fixture(autouse=True)
def _reset_installed_cache() -> Iterator[None]:
    """Clear the host-global installed-tags cache around every test.

    The cache is process-global by design (one Ollama daemon per host), so
    without this reset one test's pull/refresh would leak installed tags into
    the next test's ``available_models()`` and break isolation.
    """
    from gilbert_plugin_ollama import _installed_cache

    _installed_cache.set([])
    yield
    _installed_cache.set([])
