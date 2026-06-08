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

S8 (gilbert#40) adds **one-click pull + delete**: ``model_manager.pull``
installs a catalog quant via the runtime capability (awaited to completion)
and, on success, SEEDS the model's per-model config (``enabled=True`` +
best-effort ``context_window`` from HF metadata) through the optional
``ai_model_config`` capability so the pulled tag is immediately
chat-selectable; ``model_manager.delete`` removes an installed tag. Both are
admin-gated.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from gilbert.interfaces.ai import ModelConfig, PerModelConfigProvider
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

# The AI-backend name the local runtime (Ollama) maps to. Per-model config is
# keyed by ``(backend, model)``; a pulled tag is an ``ollama`` model, so we
# seed its config under this backend so the chat picker (which reads the
# ollama backend's available_models() filtered by per-model ``enabled``)
# surfaces it. The manager talks to the runtime through the provider-neutral
# capability, but per-model config is inherently per-backend, and the only
# runtime the manager pulls into today is Ollama (ADR-0007).
_RUNTIME_BACKEND = "ollama"

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


# Substrings that mark a pull failure as *expected* (operator-actionable, not a
# bug): an HTTP 4xx, a manifest problem, a missing repo, or a quantization
# complaint from Ollama. These get a WARNING (no traceback); everything else
# keeps the full ``logger.exception`` trace. Matched case-insensitively.
_EXPECTED_PULL_FAILURE_MARKERS = (
    "manifest",
    "quantization",
    "not found",
    "404",
    "400",
    "401",
    "403",
    "not a valid",
)


def _is_expected_pull_failure(exc: Exception) -> bool:
    """True iff ``exc`` looks like an expected, operator-actionable pull
    failure (4xx / manifest / not-found / quantization) rather than a bug."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _EXPECTED_PULL_FAILURE_MARKERS)


def _clean_pull_error(exc: Exception) -> str:
    """Return a single-line, bounded error message for a failed pull.

    Collapses whitespace and truncates so a verbose multi-line daemon error
    (or a streamed manifest dump) doesn't flood the UI toast.
    """
    msg = " ".join(str(exc).split())
    return msg[:300] if msg else exc.__class__.__name__


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
        # Optional per-model config capability (``ai_model_config``, advertised
        # by AIService, ADR-0019). Used to SEED a freshly-pulled model's
        # per-model config (enabled=True + best-effort context_window) so it's
        # immediately chat-selectable. Absent ⇒ pull still works, just no seed.
        self._per_model_config: PerModelConfigProvider | None = None
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
            # ai_model_config is optional too — a pull still installs the model
            # when it's absent, it just can't seed per-model defaults (S8).
            optional=frozenset({"host_resources", "ai_model_config"}),
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
        # ai_model_config is also optional — narrow against the provider
        # protocol (never the concrete AIService). Absent ⇒ pull seeds nothing.
        pmc = resolver.get_capability("ai_model_config")
        self._per_model_config = pmc if isinstance(pmc, PerModelConfigProvider) else None
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
        self._per_model_config = None
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
            "model_manager.pull": self._ws_pull,
            "model_manager.delete": self._ws_delete,
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
        # ``recommended_only`` sources the curated overlay server-side (the old
        # client-side post-filter over a generic page almost never surfaced the
        # ~10 curated repos, so it returned empty).
        recommended_only = bool(frame.get("recommended_only", False))
        try:
            models = await hf_catalog.search(
                query, sort, limit, recommended_only=recommended_only, client=self._http
            )
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
                    "params_b": m.params_b,
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
                    "pullable": q.pullable,
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

    async def _ws_pull(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        """Pull a model into the local runtime, then seed its per-model config.

        Admin-gated (manager actions are admin-global, ADR-0009). The client
        sends a pre-built Ollama pull ``ref`` (``hf.co/<repo_id>:<quant>``);
        as a convenience the handler also accepts ``repo_id`` + ``quant`` and
        builds the ref itself (see :meth:`_pull_ref`).

        **Progress is coarse, by design.** The pull is awaited to completion
        and this RPC returns only when the model is installed — the frontend
        shows a "pulling…" spinner while the RPC is in flight, then refreshes
        the installed list. Granular byte-level progress is intentionally out
        of scope (gilbert#40): faking a progress bar would be dishonest, so a
        spinner + completion is the deliverable.

        On success it SEEDS per-model config (when the optional
        ``ai_model_config`` capability is present): ``enabled=True`` plus a
        best-effort ``context_window`` read from the repo's HF metadata. That
        makes the pulled tag immediately chat-selectable (it now appears in
        the ollama backend's dynamic ``available_models()``, which the
        runtime refreshes on pull). A missing ``ai_model_config`` capability
        does NOT fail the pull — the model still installs, just without a seed.
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
        ref = self._pull_ref(frame)
        if not ref:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "ref (or repo_id + quant) is required",
                "code": 400,
            }
        # Pre-flight: a junk/community quant tag (e.g. ``Q8_K_P``) 400s in
        # Ollama with "not a valid quantization scheme". Reject it cleanly here
        # — never hand it to the runtime, never surface a traceback.
        quant_tag = self._pull_quant(frame, ref)
        if quant_tag and not hf_catalog.is_pullable_quant(quant_tag):
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": (
                    f"Ollama can't pull quantization '{quant_tag}' — it's not a "
                    "recognized scheme. Try a standard quant (e.g. Q4_K_M, Q8_0) "
                    "or a different repo."
                ),
                "code": 400,
            }
        try:
            await self._runtime.pull_model(ref)
        except Exception as exc:
            return self._pull_error_response(frame, ref, exc)
        # The installed tag is the ref Ollama now serves chat requests under
        # (``hf.co/<repo>:<quant>``). Seed per-model config so it's enabled
        # and carries a sensible context window.
        repo_id = str(frame.get("repo_id") or "")
        seeded_context = await self._seed_model_config(ref, repo_id)
        return {
            "type": "model_manager.pull.result",
            "ref": frame.get("id"),
            "model": ref,
            "seeded": self._per_model_config is not None,
            "context_window": seeded_context,
        }

    async def _ws_delete(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        """Delete an installed model tag from the local runtime.

        Admin-gated. ``tag`` is the installed tag from the installed list.
        On success the runtime drops it from the host-global installed cache,
        so it disappears from both the installed list and the chat picker.
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
        tag = str(frame.get("tag") or "")
        if not tag:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "tag is required",
                "code": 400,
            }
        try:
            await self._runtime.delete_model(tag)
        except Exception as exc:
            logger.exception("model_manager.delete failed for %s", tag)
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": f"delete failed: {exc}",
                "code": 502,
            }
        return {
            "type": "model_manager.delete.result",
            "ref": frame.get("id"),
            "tag": tag,
        }

    @staticmethod
    def _pull_ref(frame: dict[str, Any]) -> str:
        """Resolve the Ollama pull ref from the frame.

        Prefers a pre-built ``ref`` from the client; otherwise constructs the
        Hugging Face GGUF reference Ollama accepts —
        ``hf.co/<repo_id>:<quant>`` — from ``repo_id`` + ``quant``. Returns
        ``""`` when neither is supplied so the caller can reject the request.
        """
        ref = str(frame.get("ref") or "").strip()
        if ref:
            return ref
        repo_id = str(frame.get("repo_id") or "").strip()
        quant = str(frame.get("quant") or "").strip()
        if repo_id and quant:
            return f"hf.co/{repo_id}:{quant}"
        return ""

    @staticmethod
    def _pull_quant(frame: dict[str, Any], ref: str) -> str:
        """Extract the quant tag the pull will use, for pre-flight validation.

        Prefers an explicit ``quant`` field; otherwise reads the tag after the
        final ``:`` in the resolved ref (``hf.co/<repo>:<tag>``). Returns ``""``
        when no tag is present (a bare registry ref like ``llama3.3`` has none —
        nothing to validate, so the pull proceeds).
        """
        quant = str(frame.get("quant") or "").strip()
        if quant:
            return quant
        # ``hf.co/<repo>:<tag>`` — the tag is whatever follows the last colon,
        # but only when that colon is part of the path tail (after the host).
        if ":" in ref:
            tail = ref.rsplit("/", 1)[-1]
            if ":" in tail:
                return tail.rsplit(":", 1)[-1].strip()
        return ""

    def _pull_error_response(
        self, frame: dict[str, Any], ref: str, exc: Exception
    ) -> dict[str, Any]:
        """Build the ``gilbert.error`` for a failed pull, logging proportionate
        to how expected the failure is.

        An *expected* pull failure — a 4xx, a manifest error, a "not found", or
        a "quantization" complaint from Ollama — is operator-actionable, not a
        bug: log it at WARNING (no traceback) and return a cleaned message.
        Anything else is genuinely unexpected, so keep the full
        ``logger.exception`` traceback for debugging.
        """
        if _is_expected_pull_failure(exc):
            logger.warning("model_manager.pull failed for %s: %s", ref, exc)
        else:
            logger.exception("model_manager.pull failed for %s", ref)
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": f"pull failed: {_clean_pull_error(exc)}",
            "code": 502,
        }

    async def _seed_model_config(self, model: str, repo_id: str) -> int | None:
        """Seed per-model config for a freshly-pulled model. Returns the
        seeded context window (or ``None``).

        Best-effort and non-fatal: when the ``ai_model_config`` capability is
        absent, or reading the HF context window fails, the pull still
        succeeds — we just store less (or nothing). The seed enables the model
        (so it shows in the chat picker) and records a context window when one
        can be read from HF metadata.
        """
        if self._per_model_config is None:
            return None
        context_window: int | None = None
        if repo_id:
            try:
                context_window = await hf_catalog.fetch_context_window(repo_id, client=self._http)
            except Exception:
                logger.debug("context-window lookup failed for %s; leaving unset", repo_id)
                context_window = None
        try:
            await self._per_model_config.set_model_config(
                ModelConfig(
                    backend=_RUNTIME_BACKEND,
                    model=model,
                    enabled=True,
                    context_window=context_window,
                )
            )
        except Exception:
            logger.exception("seeding per-model config failed for %s", model)
        return context_window

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
