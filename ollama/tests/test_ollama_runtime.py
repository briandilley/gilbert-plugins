"""Tests for OllamaRuntimeService — the local_model_runtime capability.

Mocks ``httpx.AsyncClient`` so list/pull/delete are asserted to hit the
right daemon endpoints (at the server root, not under ``/v1``) without a
running Ollama.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_ollama import _installed_cache
from gilbert_plugin_ollama.ollama_ai import OllamaAI
from gilbert_plugin_ollama.ollama_runtime import OllamaRuntimeService

from gilbert.interfaces.local_models import (
    InstalledModel,
    LocalModelRuntimeProvider,
)
from gilbert.interfaces.service import ServiceResolver


class _FakeConfig:
    """Minimal ConfigurationReader-shaped stub."""

    def __init__(self, ollama_cfg: dict[str, Any]) -> None:
        self._cfg = ollama_cfg

    def get(self, path: str) -> Any:  # pragma: no cover - unused
        return None

    def get_section(self, namespace: str) -> dict[str, Any]:
        return self.get_section_safe(namespace)

    def get_section_safe(self, namespace: str) -> dict[str, Any]:
        if namespace == "ai":
            return {"backends": {"ollama": self._cfg}}
        return {}

    async def set(self, path: str, value: Any) -> dict[str, Any]:  # pragma: no cover
        return {}


class _FakeResolver(ServiceResolver):
    def __init__(self, config_svc: Any) -> None:
        self._config_svc = config_svc

    def get_capability(self, capability: str) -> Any:
        if capability == "configuration":
            return self._config_svc
        return None

    def require_capability(self, capability: str) -> Any:  # pragma: no cover
        svc = self.get_capability(capability)
        if svc is None:
            raise LookupError(capability)
        return svc

    def get_all(self, capability: str) -> list[Any]:  # pragma: no cover
        svc = self.get_capability(capability)
        return [svc] if svc is not None else []


async def _started(cfg: dict[str, Any]) -> OllamaRuntimeService:
    svc = OllamaRuntimeService()
    await svc.start(_FakeResolver(_FakeConfig(cfg)))
    return svc


def _client_cm(client: MagicMock) -> MagicMock:
    """Wrap a mock client so ``async with httpx.AsyncClient(...)`` yields it."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def test_service_info_advertises_capability() -> None:
    info = OllamaRuntimeService().service_info()
    assert "local_model_runtime" in info.capabilities
    assert info.name == "ollama_runtime"


def test_satisfies_local_model_runtime_protocol() -> None:
    assert isinstance(OllamaRuntimeService(), LocalModelRuntimeProvider)


async def test_base_url_strips_v1_from_configured() -> None:
    svc = await _started({"base_url": "http://gpu.lan:11434/v1"})
    assert svc.base_url() == "http://gpu.lan:11434"


async def test_base_url_defaults_to_localhost_root() -> None:
    svc = await _started({})
    assert svc.base_url() == "http://localhost:11434"


async def test_list_models_hits_api_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = await _started({"base_url": "http://localhost:11434/v1"})

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "models": [
            {"name": "llama3.3:latest", "size": 42_000_000_000},
            {"name": "mistral:7b"},
        ]
    }
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    monkeypatch.setattr(
        "gilbert_plugin_ollama.ollama_runtime.httpx.AsyncClient",
        lambda *a, **k: _client_cm(client),
    )

    out = await svc.list_models()
    assert out == [
        InstalledModel(tag="llama3.3:latest", size_bytes=42_000_000_000),
        InstalledModel(tag="mistral:7b", size_bytes=None),
    ]
    url = client.get.call_args[0][0]
    assert url == "http://localhost:11434/api/tags"


async def test_pull_model_posts_api_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = await _started({})

    # Stream of NDJSON progress frames, last one a success status.
    lines = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "success"}),
    ]

    resp = MagicMock()
    resp.is_error = False

    async def _aiter_lines() -> Any:
        for line in lines:
            yield line

    resp.aiter_lines = _aiter_lines

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=resp)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    client = MagicMock()
    client.stream = MagicMock(return_value=stream_cm)
    monkeypatch.setattr(
        "gilbert_plugin_ollama.ollama_runtime.httpx.AsyncClient",
        lambda *a, **k: _client_cm(client),
    )

    await svc.pull_model("hf.co/some/repo:Q4_K_M")

    method, url = client.stream.call_args[0][0], client.stream.call_args[0][1]
    assert method == "POST"
    assert url == "http://localhost:11434/api/pull"
    assert client.stream.call_args[1]["json"]["name"] == "hf.co/some/repo:Q4_K_M"


async def test_pull_model_raises_on_error_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = await _started({})
    resp = MagicMock()
    resp.is_error = False

    async def _aiter_lines() -> Any:
        yield json.dumps({"error": "pull access denied"})

    resp.aiter_lines = _aiter_lines
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=resp)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    client = MagicMock()
    client.stream = MagicMock(return_value=stream_cm)
    monkeypatch.setattr(
        "gilbert_plugin_ollama.ollama_runtime.httpx.AsyncClient",
        lambda *a, **k: _client_cm(client),
    )

    with pytest.raises(RuntimeError, match="pull access denied"):
        await svc.pull_model("nope")


async def test_delete_model_calls_api_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = await _started({})
    resp = MagicMock()
    resp.is_error = False
    client = MagicMock()
    client.request = AsyncMock(return_value=resp)
    monkeypatch.setattr(
        "gilbert_plugin_ollama.ollama_runtime.httpx.AsyncClient",
        lambda *a, **k: _client_cm(client),
    )

    await svc.delete_model("llama3.3:latest")

    method, url = client.request.call_args[0][0], client.request.call_args[0][1]
    assert method == "DELETE"
    assert url == "http://localhost:11434/api/delete"
    assert client.request.call_args[1]["json"] == {"name": "llama3.3:latest"}


# --- pull/delete keep the shared installed cache (and available_models) live ---


def _tags_resp(*names: str) -> MagicMock:
    """A ``/api/tags`` GET response carrying the given installed tag names."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"models": [{"name": n} for n in names]}
    return resp


def _pull_stream_ok() -> MagicMock:
    """A successful ``/api/pull`` stream context manager (no error frame)."""
    resp = MagicMock()
    resp.is_error = False

    async def _aiter_lines() -> Any:
        yield json.dumps({"status": "success"})

    resp.aiter_lines = _aiter_lines
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=resp)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    return stream_cm


async def test_pull_model_makes_tag_immediately_chat_selectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``pull_model`` succeeds, the runtime re-fetches ``/api/tags`` and
    publishes the result into the host-global cache — so the Ollama AI
    backend's ``available_models()`` shows the new tag *immediately*, with no
    restart. This is the "immediately chat-selectable" guarantee."""
    svc = await _started({})
    assert _installed_cache.get() == []

    # ``/api/pull`` streams success; the post-pull ``/api/tags`` GET now
    # reports the newly-installed tag.
    client = MagicMock()
    client.stream = MagicMock(return_value=_pull_stream_ok())
    client.get = AsyncMock(return_value=_tags_resp("hf.co/some/repo:Q4_K_M"))
    monkeypatch.setattr(
        "gilbert_plugin_ollama.ollama_runtime.httpx.AsyncClient",
        lambda *a, **k: _client_cm(client),
    )

    await svc.pull_model("hf.co/some/repo:Q4_K_M")

    # The shared cache reflects the pull...
    assert _installed_cache.get() == ["hf.co/some/repo:Q4_K_M"]
    # ...and a fresh AI backend instance (reading that same cache) advertises
    # the tag without ever having hit the network itself.
    backend = OllamaAI()
    ids = [m.id for m in backend.available_models()]
    assert "hf.co/some/repo:Q4_K_M" in ids


async def test_delete_model_removes_tag_from_available_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``delete_model`` succeeds, the runtime re-fetches ``/api/tags``
    and the deleted tag disappears from the shared cache → the AI backend's
    ``available_models()`` no longer advertises it."""
    # Seed the cache as if two models were installed.
    _installed_cache.set(["llama3.3:latest", "mistral:7b"])
    svc = await _started({})

    resp = MagicMock()
    resp.is_error = False
    client = MagicMock()
    client.request = AsyncMock(return_value=resp)
    # Post-delete ``/api/tags`` now reports only the surviving tag.
    client.get = AsyncMock(return_value=_tags_resp("mistral:7b"))
    monkeypatch.setattr(
        "gilbert_plugin_ollama.ollama_runtime.httpx.AsyncClient",
        lambda *a, **k: _client_cm(client),
    )

    await svc.delete_model("llama3.3:latest")

    assert _installed_cache.get() == ["mistral:7b"]
    backend = OllamaAI()
    ids = [m.id for m in backend.available_models()]
    assert "llama3.3:latest" not in ids
    assert "mistral:7b" in ids


async def test_api_key_flows_as_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured api_key (proxied Ollama) is sent as a Bearer header —
    read from the SAME ai.backends.ollama config the backend uses."""
    svc = await _started({"api_key": "proxy-secret"})

    captured: dict[str, Any] = {}

    def _factory(*a: Any, **k: Any) -> MagicMock:
        captured.update(k)
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"models": []}
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        return _client_cm(client)

    monkeypatch.setattr("gilbert_plugin_ollama.ollama_runtime.httpx.AsyncClient", _factory)
    await svc.list_models()
    assert captured["headers"]["Authorization"] == "Bearer proxy-secret"
