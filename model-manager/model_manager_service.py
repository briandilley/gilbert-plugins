"""ModelManagerService — the local-model manager's discoverable service.

The manager exists, gates on the Ollama backend being *enabled* (an
enablement dependency, ADR-0018), and — when running — lists the models
currently installed in the local runtime. It reaches the runtime **only**
through the provider-neutral ``local_model_runtime`` capability
(``LocalModelRuntimeProvider``), so it never reads the AI backend's config
or couples to the concrete Ollama plugin.

S6 (gilbert#38) adds **catalog browsing**: two admin RPCs that query the
Hugging Face Hub for GGUF repos (``catalog.search``) and a repo's
quantizations (``catalog.quants``), each tagged against Gilbert's
recommended overlay. Hardware-fit verdicts and one-click pull are later
slices that build on this.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

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

from . import hf_catalog

logger = logging.getLogger(__name__)

# Defaults for the catalog.search RPC when the SPA omits args.
_DEFAULT_SEARCH_SORT = "downloads"
_DEFAULT_SEARCH_LIMIT = 25
_MAX_SEARCH_LIMIT = 100


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
        # Shared httpx client for Hugging Face Hub catalog calls, opened in
        # start() and closed in stop(). The HF Hub needs no API key for
        # public reads, so this is a plain client.
        self._http: httpx.AsyncClient | None = None
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
        # One client reused across catalog RPCs (connection pooling). HF
        # public reads are unauthenticated.
        self._http = httpx.AsyncClient(timeout=30.0)
        self._enabled = True

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._enabled = False

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
            "model_manager.catalog.search": self._ws_catalog_search,
            "model_manager.catalog.quants": self._ws_catalog_quants,
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

    async def _ws_catalog_search(
        self, conn: WsConnectionBase, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """Search the Hugging Face Hub for GGUF models.

        Admin-gated (the manager is an admin-global tool, ADR-0009). Reads
        ``query`` / ``sort`` / ``limit`` from the frame (defensive defaults),
        delegates to the HF catalog client, and serializes each
        ``CatalogModel`` — including the ``recommended`` overlay flag — for
        the Browse list the SPA renders.
        """
        denied = require_admin(conn, frame)
        if denied is not None:
            return denied
        query = str(frame.get("query") or "")
        sort = str(frame.get("sort") or _DEFAULT_SEARCH_SORT)
        limit = self._coerce_limit(frame.get("limit"))
        try:
            models = await hf_catalog.search(query, sort, limit, client=self._http)
        except Exception as exc:
            logger.exception("model_manager.catalog.search failed")
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": f"catalog search failed: {exc}",
                "code": 502,
            }
        return {
            "type": "model_manager.catalog.search.result",
            "ref": frame.get("id"),
            "models": [
                {
                    "id": m.id,
                    "downloads": m.downloads,
                    "likes": m.likes,
                    "last_modified": m.last_modified,
                    "recommended": m.recommended,
                }
                for m in models
            ],
        }

    async def _ws_catalog_quants(
        self, conn: WsConnectionBase, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """List a Hugging Face repo's GGUF quantizations with per-quant sizes.

        Admin-gated. ``model_id`` is required (the repo id from a search
        result); each ``Quant`` is serialized to ``filename`` /
        ``quant_label`` / ``size_bytes`` for the per-row expand in Browse.
        """
        denied = require_admin(conn, frame)
        if denied is not None:
            return denied
        model_id = str(frame.get("model_id") or "")
        if not model_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "model_id is required",
                "code": 400,
            }
        try:
            quants = await hf_catalog.list_quants(model_id, client=self._http)
        except Exception as exc:
            logger.exception("model_manager.catalog.quants failed")
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": f"catalog quants failed: {exc}",
                "code": 502,
            }
        return {
            "type": "model_manager.catalog.quants.result",
            "ref": frame.get("id"),
            "model_id": model_id,
            "quants": [
                {
                    "filename": q.filename,
                    "quant_label": q.quant_label,
                    "size_bytes": q.size_bytes,
                }
                for q in quants
            ],
        }

    @staticmethod
    def _coerce_limit(raw: Any) -> int:
        """Clamp a requested search limit to a sane, bounded integer."""
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            return _DEFAULT_SEARCH_LIMIT
        if limit <= 0:
            return _DEFAULT_SEARCH_LIMIT
        return min(limit, _MAX_SEARCH_LIMIT)
