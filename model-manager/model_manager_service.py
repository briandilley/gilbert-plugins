"""ModelManagerService — the local-model manager's discoverable service.

Skeleton slice (S5): the manager exists, gates on the Ollama backend being
*enabled* (an enablement dependency, ADR-0018), and — when running — lists
the models currently installed in the local runtime. It reaches the runtime
**only** through the provider-neutral ``local_model_runtime`` capability
(``LocalModelRuntimeProvider``), so it never reads the AI backend's config
or couples to the concrete Ollama plugin. Hugging Face catalog browsing,
hardware-fit verdicts, and one-click pull are later slices that build on
this shell.
"""

from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.local_models import LocalModelRuntimeProvider
from gilbert.interfaces.service import (
    EnablementDep,
    Service,
    ServiceInfo,
    ServiceResolver,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.ws import RpcHandler, WsConnectionBase, require_admin

logger = logging.getLogger(__name__)


class ModelManagerService(Service):
    """Drives the in-app local-model manager.

    Advertises ``model_manager`` (consumed by the plugin's ``UIRoute``'s
    ``requires_capability`` so the Models page only mounts when the manager
    is live) and declares an enablement dependency on the ``ollama`` backend
    via the ``ai_chat`` service. When Ollama is disabled the service is left
    *disabled with a reason* (a Settings badge + toast) rather than started.
    """

    # Configurable
    config_namespace = "model-manager"
    config_category = "Model Manager"

    def __init__(self) -> None:
        self._resolver: ServiceResolver | None = None
        self._runtime: LocalModelRuntimeProvider | None = None
        # Toggleable service — flipped on in start() once the enablement
        # dependency is satisfied and the runtime capability resolves.
        self._enabled = False

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="model_manager",
            capabilities=frozenset({"model_manager", "ws_handlers"}),
            requires=frozenset({"local_model_runtime"}),
            requires_enabled=(EnablementDep(capability="ai_chat", backend="ollama"),),
            toggleable=True,
            toggle_description=("Browse, fit-check, and pull local models via Ollama."),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        # The ``local_model_runtime`` capability is a hard ``requires`` — the
        # service manager only starts us once it's published, so resolve it
        # eagerly and narrow against the provider-neutral protocol (never the
        # concrete Ollama class).
        runtime = resolver.require_capability("local_model_runtime")
        if not isinstance(runtime, LocalModelRuntimeProvider):
            raise RuntimeError(
                "local_model_runtime capability does not satisfy LocalModelRuntimeProvider"
            )
        self._runtime = runtime
        self._enabled = True

    # ------------------------------------------------------------------
    # Configurable
    # ------------------------------------------------------------------

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Enable the local model manager. Requires the Ollama "
                    "AI backend to be enabled — the manager drives "
                    "pull/list/delete through it."
                ),
                default=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        # Cache config values here (the protocol requires reading the active
        # value from cached state, never from a module-level default). The
        # skeleton has no tunables of its own yet beyond the toggle, which
        # the Services section owns; cache it for completeness so later
        # slices read ``self._config_enabled`` rather than re-parsing.
        self._config_enabled = bool(config.get("enabled", True))

    # ------------------------------------------------------------------
    # WsHandlerProvider
    # ------------------------------------------------------------------

    def get_ws_handlers(self) -> dict[str, RpcHandler]:
        return {
            "model_manager.installed.list": self._ws_installed_list,
        }

    async def _ws_installed_list(
        self, conn: WsConnectionBase, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """Return the models currently installed in the local runtime.

        Admin-gated — the manager is an admin-global tool (multi-user
        isolation, ADR-0009). Serializes each ``InstalledModel`` down to its
        ``tag`` + ``size_bytes`` for the installed-models list the SPA page
        renders.
        """
        denied = require_admin(conn, frame)
        if denied is not None:
            return denied
        if self._runtime is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "local model runtime unavailable",
                "code": 503,
            }
        try:
            models = await self._runtime.list_models()
        except Exception as exc:
            logger.exception("model_manager.installed.list failed")
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": f"failed to list installed models: {exc}",
                "code": 502,
            }
        return {
            "type": "model_manager.installed.list.result",
            "ref": frame.get("id"),
            "models": [{"tag": m.tag, "size_bytes": m.size_bytes} for m in models],
        }
