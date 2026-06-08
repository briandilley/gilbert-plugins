"""Ollama local-model runtime service.

Implements the provider-neutral :class:`LocalModelRuntimeProvider`
capability (``local_model_runtime``) so a separate local-model **manager**
plugin can list / pull / delete installed Ollama tags and learn the
runtime's resolved ``base_url`` *without* reading the AI backend's config
or coupling to the concrete Ollama backend class.

Where the runtime lives is the single source of truth: this service reads
``base_url`` / ``api_key`` from the **same** ``ai.backends.ollama.*``
config the AI backend uses, via the ``ConfigurationReader`` capability —
it never reads another plugin's storage. The Ollama daemon's native API
(``/api/tags``, ``/api/pull``, ``/api/delete``) sits at the server root,
one level above the OpenAI-compatible ``/v1`` path the AI backend talks to.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

from gilbert.interfaces.configuration import ConfigurationReader
from gilbert.interfaces.local_models import InstalledModel, PullProgress
from gilbert.interfaces.service import (
    Service,
    ServiceInfo,
    ServiceResolver,
)

from . import _installed_cache

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:11434/v1"
# Pulls can be large (multi-GB) and slow; give the streamed request plenty
# of headroom rather than the backend's chat-sized 120s.
_PULL_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
_DEFAULT_TIMEOUT = httpx.Timeout(30.0)


class OllamaRuntimeService(Service):
    """Drives the local Ollama daemon's installed-model lifecycle.

    Advertises ``local_model_runtime`` and satisfies
    ``LocalModelRuntimeProvider`` structurally (``list_models`` /
    ``pull_model`` / ``delete_model`` / ``base_url``).
    """

    def __init__(self) -> None:
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="ollama_runtime",
            capabilities=frozenset({"local_model_runtime"}),
            optional=frozenset({"configuration"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

    # --- Config resolution (reads the AI backend's own config) ---

    def _backend_config(self) -> dict[str, Any]:
        """Resolve ``ai.backends.ollama.*`` live via ``ConfigurationReader``.

        Returns ``{}`` when configuration isn't available so callers fall
        back to the localhost default — never reaches into other storage.
        """
        if self._resolver is None:
            return {}
        config_svc = self._resolver.get_capability("configuration")
        if not isinstance(config_svc, ConfigurationReader):
            return {}
        section = config_svc.get_section_safe("ai") or {}
        backends = section.get("backends", {})
        if not isinstance(backends, dict):
            return {}
        cfg = backends.get("ollama", {})
        return cfg if isinstance(cfg, dict) else {}

    def _root_url(self) -> str:
        """Daemon root URL (where ``/api/*`` lives), stripping a trailing ``/v1``."""
        cfg = self._backend_config()
        base = str(cfg.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        api_key = str(self._backend_config().get("api_key") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    # --- LocalModelRuntimeProvider ---

    def base_url(self) -> str:
        """Return the runtime's resolved base URL (the daemon root)."""
        return self._root_url()

    async def list_models(self) -> list[InstalledModel]:
        """Return installed tags via ``GET /api/tags``."""
        url = f"{self._root_url()}/api/tags"
        async with httpx.AsyncClient(headers=self._headers(), timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        out: list[InstalledModel] = []
        if isinstance(data, dict):
            models = data.get("models")
            if isinstance(models, list):
                for entry in models:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name") or entry.get("model")
                    if not isinstance(name, str) or not name:
                        continue
                    raw_size = entry.get("size")
                    size = int(raw_size) if isinstance(raw_size, (int, float)) else None
                    out.append(InstalledModel(tag=name, size_bytes=size))
        return out

    async def _refresh_installed_cache(self) -> None:
        """Re-fetch ``/api/tags`` and publish the tags into the shared cache.

        Called after a successful ``pull_model`` / ``delete_model`` so the
        Ollama AI backend's sync ``available_models()`` — which reads the same
        host-global ``_installed_cache`` — reflects the change *immediately*,
        making a freshly-pulled model chat-selectable without a restart.
        Best-effort: a transient ``/api/tags`` failure here leaves the cache
        as it was rather than failing the pull/delete the user already
        completed (it'll resync on the next refresh).
        """
        try:
            installed = await self.list_models()
        except Exception as exc:
            logger.debug("post-op /api/tags refresh failed: %s", exc)
            return
        _installed_cache.set([m.tag for m in installed])

    async def pull_model(
        self,
        ref: str,
        on_progress: Callable[[PullProgress], None] | None = None,
    ) -> None:
        """Pull a model via ``POST /api/pull``, awaiting completion.

        ``ref`` is any reference Ollama accepts — a registry tag
        (``llama3.3``) or a Hugging Face GGUF reference
        (``hf.co/<repo>:<quant>``). Ollama streams newline-delimited JSON
        progress frames; we drain the stream until it ends and raise if a
        frame reports an ``error``.

        For each progress frame, ``on_progress`` (when supplied) is invoked
        with a :class:`PullProgress` carrying Ollama's ``status`` and, when
        present, ``completed`` / ``total`` byte counts. It's best-effort: a
        callback that raises is logged and ignored so it can never fail the
        pull. This is what lets the manager keep the (long) pull RPC alive past
        a fixed client timeout.
        """
        url = f"{self._root_url()}/api/pull"
        body = {"name": ref, "stream": True}
        async with httpx.AsyncClient(headers=self._headers(), timeout=_PULL_TIMEOUT) as client:
            async with client.stream("POST", url, json=body) as resp:
                if resp.is_error:
                    err_bytes = await resp.aread()
                    raise RuntimeError(
                        f"Ollama pull failed ({resp.status_code}): "
                        f"{err_bytes.decode('utf-8', errors='replace')[:500]}"
                    )
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("error"):
                        raise RuntimeError(f"Ollama pull error: {frame['error']}")
                    if on_progress is not None:
                        self._emit_progress(frame, on_progress)
        # Pull succeeded — resync the host-global installed cache so the AI
        # backend's available_models() shows the new tag immediately.
        await self._refresh_installed_cache()

    @staticmethod
    def _emit_progress(
        frame: dict[str, Any],
        on_progress: Callable[[PullProgress], None],
    ) -> None:
        """Translate one Ollama pull frame into a ``PullProgress`` callback.

        Best-effort and swallowing: a misbehaving callback must never abort an
        in-flight pull the user is waiting on.
        """
        completed = frame.get("completed")
        total = frame.get("total")
        progress = PullProgress(
            status=str(frame.get("status") or ""),
            completed=int(completed) if isinstance(completed, (int, float)) else None,
            total=int(total) if isinstance(total, (int, float)) else None,
        )
        try:
            on_progress(progress)
        except Exception:
            logger.debug("pull on_progress callback raised; ignoring", exc_info=True)

    async def delete_model(self, tag: str) -> None:
        """Delete an installed tag via ``DELETE /api/delete``."""
        url = f"{self._root_url()}/api/delete"
        async with httpx.AsyncClient(headers=self._headers(), timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.request("DELETE", url, json={"name": tag})
            if resp.is_error:
                raise RuntimeError(f"Ollama delete failed ({resp.status_code}): {resp.text[:500]}")
        # Delete succeeded — resync the host-global installed cache so the AI
        # backend's available_models() drops the tag immediately.
        await self._refresh_installed_cache()
