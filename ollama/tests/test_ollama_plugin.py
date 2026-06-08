"""Tests for the ollama plugin's enablement-aware runtime dependency."""

from __future__ import annotations

from typing import Any

from gilbert_plugin_ollama.plugin import OllamaPlugin, create_plugin


def _plugin() -> OllamaPlugin:
    p = create_plugin()
    assert isinstance(p, OllamaPlugin)
    return p


def test_metadata_provides_runtime_capability() -> None:
    meta = _plugin().metadata()
    assert "ollama_ai" in meta.provides
    assert "local_model_runtime" in meta.provides


def test_runtime_dependencies_none_config_returns_empty() -> None:
    assert _plugin().runtime_dependencies(None) == []


def test_runtime_dependencies_disabled_returns_empty() -> None:
    config = {"ai": {"settings": {"backends": {"ollama": {"enabled": False}}}}}
    assert _plugin().runtime_dependencies(config) == []


def test_runtime_dependencies_missing_backend_returns_empty() -> None:
    config: dict[str, Any] = {"ai": {"settings": {"backends": {}}}}
    assert _plugin().runtime_dependencies(config) == []


def test_runtime_dependencies_enabled_returns_daemon_dep() -> None:
    config = {"ai": {"settings": {"backends": {"ollama": {"enabled": True}}}}}
    deps = _plugin().runtime_dependencies(config)
    assert len(deps) == 1
    dep = deps[0]
    assert dep.name == "ollama-daemon"
    # Exercises the daemon (GET /api/tags), not a path probe.
    assert "/api/tags" in dep.check_cmd
    assert dep.check_cmd.startswith("curl ")
    # Manual install only.
    assert dep.auto_install_cmd == ""
    assert "ollama.com" in dep.install_hint


def test_runtime_dependencies_enabled_flattened_config() -> None:
    """The live entity config flattens backends to ai.backends.* (no
    'settings' nesting) — the dep must still resolve."""
    config = {"ai": {"backends": {"ollama": {"enabled": True}}}}
    deps = _plugin().runtime_dependencies(config)
    assert len(deps) == 1
    assert deps[0].name == "ollama-daemon"


def test_runtime_dependencies_uses_configured_base_url() -> None:
    config = {
        "ai": {
            "settings": {
                "backends": {
                    "ollama": {
                        "enabled": True,
                        "base_url": "http://gpu.lan:11434/v1",
                    }
                }
            }
        }
    }
    deps = _plugin().runtime_dependencies(config)
    assert "http://gpu.lan:11434/api/tags" in deps[0].check_cmd
