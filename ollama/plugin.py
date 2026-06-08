"""Ollama plugin — registers the local Ollama AI backend + runtime service.

``setup()`` does two things:

- Side-effect-imports ``ollama_ai`` so the ABC's ``__init_subclass__``
  hook registers the ``OllamaAI`` backend.
- Registers ``OllamaRuntimeService`` (the ``local_model_runtime``
  capability) so a separate local-model manager can drive pull/list/delete
  without reading this backend's config.

It also declares an **enablement-aware** runtime dependency on the Ollama
daemon (ADR-0008): the dep is returned only when the ``ollama`` backend is
enabled in the config ``doctor`` passes in, so operators who don't use
Ollama aren't nagged to install it.
"""

from __future__ import annotations

from typing import Any

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    RuntimeDependency,
)

_DEFAULT_BASE_URL = "http://localhost:11434/v1"


class OllamaPlugin(Plugin):
    """Registers the Ollama AI backend and the local-model runtime service."""

    def __init__(self) -> None:
        self._service: object | None = None

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="ollama",
            version="1.1.0",
            description="Local Ollama AI backend + local-model runtime service",
            provides=["ollama_ai", "local_model_runtime"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import ollama_ai  # noqa: F401 — triggers backend registration
        from .ollama_runtime import OllamaRuntimeService

        self._service = OllamaRuntimeService()
        context.services.register(self._service)

    async def teardown(self) -> None:
        pass  # Service lifecycle handled by ServiceManager.stop_all()

    def runtime_dependencies(self, config: dict[str, Any] | None = None) -> list[RuntimeDependency]:
        """Declare the Ollama daemon dep — only when the backend is enabled.

        ``doctor`` passes the resolved config (ADR-0008). We read the
        ``ollama`` backend's ``enabled`` flag from it and, only when truthy,
        return a single dependency whose ``check_cmd`` *exercises* the
        daemon (``GET /api/tags``) rather than probing a path. Install is
        manual (``auto_install_cmd`` empty) since it needs root.
        """
        if not self._ollama_backend_enabled(config):
            return []

        base_url = self._configured_base_url(config)
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        tags_url = f"{root.rstrip('/')}/api/tags"

        return [
            RuntimeDependency(
                name="ollama-daemon",
                description=(
                    "The Ollama daemon must be running and reachable for the "
                    "ollama AI backend and the local-model runtime to work. "
                    "Checked by hitting the daemon's /api/tags endpoint."
                ),
                check_cmd=f"curl -fsS {tags_url} >/dev/null",
                install_hint=(
                    "Install from https://ollama.com/download "
                    "(Linux: 'curl -fsSL https://ollama.com/install.sh | sh'), "
                    "then 'ollama serve'."
                ),
                auto_install_cmd="",
            )
        ]

    @staticmethod
    def _ollama_backend_config(config: dict[str, Any] | None) -> dict[str, Any]:
        """Pull ``ai.backends.ollama.*`` out of the resolved config dict.

        ``doctor`` loads the bootstrap config where AI backend settings nest
        under ``ai.settings.backends.ollama``; the live entity config flattens
        them to ``ai.backends.ollama``. Accept either so the check is robust
        regardless of which shape ``doctor`` hands us.
        """
        if not isinstance(config, dict):
            return {}
        ai = config.get("ai")
        if not isinstance(ai, dict):
            return {}
        # Prefer the bootstrap ``settings`` nesting, fall back to flattened.
        for container in (ai.get("settings"), ai):
            if not isinstance(container, dict):
                continue
            backends = container.get("backends")
            if isinstance(backends, dict):
                cfg = backends.get("ollama")
                if isinstance(cfg, dict):
                    return cfg
        return {}

    @classmethod
    def _ollama_backend_enabled(cls, config: dict[str, Any] | None) -> bool:
        return bool(cls._ollama_backend_config(config).get("enabled"))

    @classmethod
    def _configured_base_url(cls, config: dict[str, Any] | None) -> str:
        cfg = cls._ollama_backend_config(config)
        return str(cfg.get("base_url") or _DEFAULT_BASE_URL)


def create_plugin() -> Plugin:
    return OllamaPlugin()
