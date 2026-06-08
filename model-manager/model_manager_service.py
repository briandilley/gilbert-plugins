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
recommended overlay.

S7 (gilbert#39) adds **hardware-fit**: ``catalog.quants`` attaches a per-quant
``fit`` verdict (the manager's policy, in :mod:`.fit`) computed from the
``host_resources`` capability and whether the runtime's ``base_url`` is local
or remote, and a ``host.resources`` RPC surfaces the host summary so the UI
can explain an "unknown" verdict.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.host_resources import HostResources, HostResourcesProvider
from gilbert.interfaces.local_models import LocalModelRuntimeProvider
from gilbert.interfaces.service import (
    EnablementDep,
    Service,
    ServiceInfo,
    ServiceResolver,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.ws import RpcHandler, WsConnectionBase, require_admin

from . import fit, hf_catalog

logger = logging.getLogger(__name__)

# Defaults for the catalog.search RPC when the SPA omits args.
_DEFAULT_SEARCH_SORT = "downloads"
_DEFAULT_SEARCH_LIMIT = 25
_MAX_SEARCH_LIMIT = 100

# Hostnames that mean "Ollama runs on this very box" — the only case where the
# localhost-only host-resources probe describes the right machine. Anything
# else is remote and yields an "unknown" fit (S7, gilbert#39). An empty host
# (e.g. a bare/relative base_url) is treated as local rather than guessed.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


def _is_remote_base_url(base_url: str) -> bool:
    """Return True iff ``base_url`` points at a host other than this box.

    Parses the URL's hostname (``urlparse`` strips the port and the ``[...]``
    IPv6 brackets) and treats only the localhost forms in ``_LOCAL_HOSTS`` as
    local. An empty or unparseable base_url is treated as local — better to
    show a (possibly stale) verdict than to flag a normal localhost setup as
    remote. Errors collapse to "local" for the same reason.
    """
    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return False
    if host is None:
        # No scheme/netloc (e.g. a bare host:port); fall back to the raw value
        # minus any trailing port so "localhost:11434" still reads as local.
        host = base_url.rsplit(":", 1)[0] if base_url else ""
    return host.lower() not in _LOCAL_HOSTS


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
        # Optional host-resources capability (S7). Defensive: it may be absent
        # (a stripped-down install, the core service disabled), in which case
        # fit verdicts are "unknown" rather than a crash.
        self._host_resources: HostResourcesProvider | None = None
        # Whether the runtime's resolved base_url points off-box. Fit reflects
        # the machine Ollama runs on; only localhost can be probed, so a remote
        # runtime yields "unknown". Computed once in start() from base_url().
        self._remote = False
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
            # host_resources is optional — the fit verdict degrades to
            # "unknown" when it's absent rather than failing to start (S7).
            optional=frozenset({"host_resources"}),
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
        # host_resources is an *optional* capability — resolve defensively and
        # narrow against the provider protocol (never the concrete service).
        # Absent ⇒ self._host_resources stays None ⇒ fit verdicts are unknown.
        host = resolver.get_capability("host_resources")
        self._host_resources = host if isinstance(host, HostResourcesProvider) else None
        # Derive remote-ness from where the runtime actually lives. The
        # localhost-only probe only describes this box, so a remote base_url
        # means we can't honestly judge fit.
        self._remote = _is_remote_base_url(runtime.base_url())
        # One client reused across catalog RPCs (connection pooling). HF
        # public reads are unauthenticated.
        self._http = httpx.AsyncClient(timeout=30.0)
        self._enabled = True

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._host_resources = None
        self._remote = False
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
            "model_manager.host.resources": self._ws_host_resources,
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
        # Fetch the host snapshot once for the whole repo (not per quant), then
        # attach a fit verdict to each gguf. A quant whose size HF didn't report
        # (size_bytes is None) is "unknown" — we can't judge a fit without it.
        host = await self._host_snapshot()
        quant_rows: list[dict[str, Any]] = []
        for q in quants:
            if q.size_bytes is None:
                verdict = fit.UNKNOWN
            else:
                verdict = fit.fit_verdict(q.size_bytes, host, remote=self._remote)
            quant_rows.append(
                {
                    "filename": q.filename,
                    "quant_label": q.quant_label,
                    "size_bytes": q.size_bytes,
                    "fit": verdict,
                }
            )
        return {
            "type": "model_manager.catalog.quants.result",
            "ref": frame.get("id"),
            "model_id": model_id,
            "quants": quant_rows,
        }

    async def _ws_host_resources(
        self, conn: WsConnectionBase, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a host summary the SPA renders + uses to explain "unknown" fit.

        Admin-gated. Reports total/available RAM, the per-GPU VRAM list, and a
        ``remote`` flag (the runtime's ``base_url`` is off-box). When the
        host-resources capability is absent or its probe fails the RAM figures
        are ``null`` and ``gpus`` is empty — a valid "unknown" summary, not an
        error, so the UI can say "remote Ollama — fit unknown" or "host
        resources unavailable".
        """
        denied = require_admin(conn, frame)
        if denied is not None:
            return denied
        host = await self._host_snapshot()
        gpus = (
            [{"name": g.name, "total_vram_bytes": g.total_vram_bytes} for g in host.gpus]
            if host is not None
            else []
        )
        return {
            "type": "model_manager.host.resources.result",
            "ref": frame.get("id"),
            "total_ram_bytes": host.total_ram_bytes if host is not None else None,
            "available_ram_bytes": host.available_ram_bytes if host is not None else None,
            "gpus": gpus,
            "remote": self._remote,
        }

    async def _host_snapshot(self) -> HostResources | None:
        """Read the host-resources snapshot once, degrading to ``None``.

        Returns ``None`` when the capability is absent or its (best-effort)
        probe raises — both feed the policy as "unknown" rather than crashing
        a catalog or host RPC.
        """
        if self._host_resources is None:
            return None
        try:
            return await self._host_resources.get_host_resources()
        except Exception:
            logger.warning("host_resources probe failed; fit verdicts will be unknown")
            return None

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
