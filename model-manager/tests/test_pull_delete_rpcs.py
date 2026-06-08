"""Pull/delete RPC tests (S8, gilbert#40) — at the service-method seam.

Fakes the ``local_model_runtime`` and ``ai_model_config`` capabilities and
asserts external behavior:

- ``model_manager.pull`` calls the runtime ``pull_model`` with the right ref
  and then SEEDS per-model config (``backend="ollama"``, ``model=<ref>``,
  ``enabled=True``) through the faked ``PerModelConfigProvider``.
- pull tolerates a missing ``ai_model_config`` capability (still pulls).
- ``model_manager.delete`` calls the runtime ``delete_model``.

The HF context-window lookup is monkeypatched so these don't hit the network.
"""

from __future__ import annotations

from typing import Any

import pytest
from gilbert_plugin_model_manager import hf_catalog
from gilbert_plugin_model_manager.model_manager_service import ModelManagerService

from gilbert.interfaces.ai import ModelConfig, PerModelConfigProvider
from gilbert.interfaces.local_models import InstalledModel


class _FakeRuntime:
    """Duck-typed ``LocalModelRuntimeProvider`` recording pull/delete calls."""

    def __init__(self) -> None:
        self.pulled: list[str] = []
        self.deleted: list[str] = []

    async def list_models(self) -> list[InstalledModel]:  # pragma: no cover - unused
        return []

    async def pull_model(self, ref: str) -> None:
        self.pulled.append(ref)

    async def delete_model(self, tag: str) -> None:
        self.deleted.append(tag)

    def base_url(self) -> str:
        return "http://localhost:11434"


class _ExplodingRuntime(_FakeRuntime):
    async def pull_model(self, ref: str) -> None:
        raise RuntimeError("pull access denied")


class _FakePerModelConfig:
    """Duck-typed ``PerModelConfigProvider`` recording set calls.

    ``isinstance`` against the ``@runtime_checkable`` protocol passes because
    all three methods exist.
    """

    def __init__(self) -> None:
        self.set_calls: list[ModelConfig] = []

    async def get_model_config(self, backend: str, model: str) -> ModelConfig:
        return ModelConfig(backend=backend, model=model)

    async def set_model_config(self, cfg: ModelConfig) -> None:
        self.set_calls.append(cfg)

    async def list_model_configs(self) -> list[ModelConfig]:  # pragma: no cover - unused
        return list(self.set_calls)


class _FakeResolver:
    def __init__(self, runtime: Any, per_model: Any = None) -> None:
        self._runtime = runtime
        self._per_model = per_model

    def get_capability(self, name: str) -> Any:
        if name == "local_model_runtime":
            return self._runtime
        if name == "ai_model_config":
            return self._per_model
        return None

    def require_capability(self, name: str) -> Any:
        if name == "local_model_runtime":
            return self._runtime
        raise KeyError(name)

    def get_all(self, name: str) -> list[Any]:  # pragma: no cover - unused
        return []


class _Conn:
    """Minimal WS connection stub. ``user_level == 0`` ⇒ admin."""

    def __init__(self, user_level: int = 0) -> None:
        self.user_level = user_level


async def _start(runtime: Any, per_model: Any = None) -> ModelManagerService:
    svc = ModelManagerService()
    await svc.start(_FakeResolver(runtime, per_model))
    return svc


# --- handler registration + capability declaration ------------------------


def test_pull_and_delete_handlers_registered() -> None:
    handlers = ModelManagerService().get_ws_handlers()
    assert "model_manager.pull" in handlers
    assert "model_manager.delete" in handlers


def test_service_declares_ai_model_config_optional() -> None:
    info = ModelManagerService().service_info()
    assert "ai_model_config" in info.optional


# --- pull → seed ----------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_calls_runtime_and_seeds_per_model_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FakeRuntime()
    pmc = _FakePerModelConfig()
    svc = await _start(runtime, pmc)

    async def _ctx(model_id: str, **kw: Any) -> int | None:
        assert model_id == "bartowski/Qwen2.5-7B-Instruct-GGUF"
        return 32768

    monkeypatch.setattr(hf_catalog, "fetch_context_window", _ctx)

    ref = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"
    resp = await svc._ws_pull(
        _Conn(0),
        {
            "id": "p1",
            "ref": ref,
            "repo_id": "bartowski/Qwen2.5-7B-Instruct-GGUF",
            "quant": "Q4_K_M",
        },
    )

    assert resp["type"] == "model_manager.pull.result"
    assert resp["ref"] == "p1"
    assert resp["model"] == ref
    # The runtime was driven with the exact ref the client sent.
    assert runtime.pulled == [ref]
    # Per-model config was seeded: ollama backend, the installed tag, enabled,
    # and the best-effort context window from HF.
    assert len(pmc.set_calls) == 1
    cfg = pmc.set_calls[0]
    assert cfg.backend == "ollama"
    assert cfg.model == ref
    assert cfg.enabled is True
    assert cfg.context_window == 32768
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_builds_ref_from_repo_and_quant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the client omits ``ref``, the handler builds
    ``hf.co/<repo_id>:<quant>`` itself."""
    runtime = _FakeRuntime()
    svc = await _start(runtime, _FakePerModelConfig())

    async def _ctx(model_id: str, **kw: Any) -> int | None:
        return None

    monkeypatch.setattr(hf_catalog, "fetch_context_window", _ctx)

    resp = await svc._ws_pull(
        _Conn(0),
        {"id": "p2", "repo_id": "owner/Repo-GGUF", "quant": "Q8_0"},
    )
    assert runtime.pulled == ["hf.co/owner/Repo-GGUF:Q8_0"]
    assert resp["model"] == "hf.co/owner/Repo-GGUF:Q8_0"
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_tolerates_missing_ai_model_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``ai_model_config`` capability ⇒ the model still pulls; the seed is
    simply skipped (no crash)."""
    runtime = _FakeRuntime()
    svc = await _start(runtime, per_model=None)  # capability absent

    async def _ctx(model_id: str, **kw: Any) -> int | None:  # pragma: no cover
        return 4096

    monkeypatch.setattr(hf_catalog, "fetch_context_window", _ctx)

    ref = "hf.co/owner/Repo-GGUF:Q4_K_M"
    resp = await svc._ws_pull(_Conn(0), {"id": "p3", "ref": ref, "repo_id": "owner/Repo-GGUF"})
    assert runtime.pulled == [ref]
    assert resp["type"] == "model_manager.pull.result"
    assert resp["seeded"] is False
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_context_window_none_when_metadata_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable context window leaves the seed's context_window None — it
    does NOT fail the pull."""
    runtime = _FakeRuntime()
    pmc = _FakePerModelConfig()
    svc = await _start(runtime, pmc)

    async def _ctx(model_id: str, **kw: Any) -> int | None:
        return None

    monkeypatch.setattr(hf_catalog, "fetch_context_window", _ctx)

    ref = "hf.co/owner/Repo-GGUF:Q4_K_M"
    await svc._ws_pull(_Conn(0), {"id": "p4", "ref": ref, "repo_id": "owner/Repo-GGUF"})
    assert runtime.pulled == [ref]
    assert pmc.set_calls[0].context_window is None
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_surfaces_runtime_failure() -> None:
    runtime = _ExplodingRuntime()
    svc = await _start(runtime, _FakePerModelConfig())
    resp = await svc._ws_pull(_Conn(0), {"id": "p5", "ref": "hf.co/owner/Repo:Q4_K_M"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 502
    assert "pull access denied" in resp["error"]
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_requires_a_ref() -> None:
    svc = await _start(_FakeRuntime(), _FakePerModelConfig())
    resp = await svc._ws_pull(_Conn(0), {"id": "p6"})  # no ref/repo/quant
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 400
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_is_admin_gated() -> None:
    runtime = _FakeRuntime()
    svc = await _start(runtime, _FakePerModelConfig())
    resp = await svc._ws_pull(_Conn(1), {"id": "p7", "ref": "hf.co/x/y:Q4_K_M"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403
    # Denied before touching the runtime.
    assert runtime.pulled == []
    await svc.stop()


# --- delete ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_calls_runtime_delete() -> None:
    runtime = _FakeRuntime()
    svc = await _start(runtime, _FakePerModelConfig())
    resp = await svc._ws_delete(_Conn(0), {"id": "d1", "tag": "llama3.3:latest"})
    assert resp["type"] == "model_manager.delete.result"
    assert resp["tag"] == "llama3.3:latest"
    assert runtime.deleted == ["llama3.3:latest"]
    await svc.stop()


@pytest.mark.asyncio
async def test_delete_requires_a_tag() -> None:
    svc = await _start(_FakeRuntime(), _FakePerModelConfig())
    resp = await svc._ws_delete(_Conn(0), {"id": "d2"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 400
    await svc.stop()


@pytest.mark.asyncio
async def test_delete_is_admin_gated() -> None:
    runtime = _FakeRuntime()
    svc = await _start(runtime, _FakePerModelConfig())
    resp = await svc._ws_delete(_Conn(1), {"id": "d3", "tag": "x"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403
    assert runtime.deleted == []
    await svc.stop()


@pytest.mark.asyncio
async def test_delete_surfaces_runtime_failure() -> None:
    class _ExplodingDelete(_FakeRuntime):
        async def delete_model(self, tag: str) -> None:
            raise RuntimeError("no such model")

    svc = await _start(_ExplodingDelete(), _FakePerModelConfig())
    resp = await svc._ws_delete(_Conn(0), {"id": "d4", "tag": "x"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 502
    assert "no such model" in resp["error"]
    await svc.stop()


@pytest.mark.asyncio
async def test_seeded_config_satisfies_protocol() -> None:
    """Sanity: the fake provider really is a ``PerModelConfigProvider`` (the
    service narrows against this protocol, so the test must too)."""
    assert isinstance(_FakePerModelConfig(), PerModelConfigProvider)
