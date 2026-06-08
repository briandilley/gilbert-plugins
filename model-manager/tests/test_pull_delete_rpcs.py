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
from gilbert.interfaces.local_models import InstalledModel, PullProgress


class _FakeRuntime:
    """Duck-typed ``LocalModelRuntimeProvider`` recording pull/delete calls."""

    def __init__(self) -> None:
        self.pulled: list[str] = []
        self.deleted: list[str] = []

    async def list_models(self) -> list[InstalledModel]:  # pragma: no cover - unused
        return []

    async def pull_model(self, ref: str, on_progress: Any = None) -> None:
        self.pulled.append(ref)

    async def delete_model(self, tag: str) -> None:
        self.deleted.append(tag)

    def base_url(self) -> str:
        return "http://localhost:11434"


class _ExplodingRuntime(_FakeRuntime):
    async def pull_model(self, ref: str, on_progress: Any = None) -> None:
        raise RuntimeError("pull access denied")


class _ProgressRuntime(_FakeRuntime):
    """Reports two distinct-status progress frames through the callback."""

    async def pull_model(self, ref: str, on_progress: Any = None) -> None:
        self.pulled.append(ref)
        if on_progress is not None:
            on_progress(PullProgress(status="pulling manifest"))
            on_progress(
                PullProgress(status="pulling sha256:abc", completed=10, total=100)
            )


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
    """Minimal WS connection stub. ``user_level == 0`` ⇒ admin.

    Records frames the handler pushes via ``enqueue`` (the pull-progress
    keepalive events) so tests can assert on them.
    """

    def __init__(self, user_level: int = 0) -> None:
        self.user_level = user_level
        self.enqueued: list[dict[str, Any]] = []

    def enqueue(self, frame: dict[str, Any]) -> None:
        self.enqueued.append(frame)


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
async def test_pull_emits_progress_events_keyed_by_ref() -> None:
    """While pulling, the handler pushes ``model_manager.pull.progress`` events
    to the requesting connection, each carrying the originating RPC's ``ref`` so
    the frontend resets THAT pending RPC's deadline (the timeout keepalive)."""
    svc = await _start(_ProgressRuntime(), per_model=None)
    conn = _Conn(0)

    resp = await svc._ws_pull(conn, {"id": "p-prog", "ref": "hf.co/owner/Repo:Q4_K_M"})
    assert resp["type"] == "model_manager.pull.result"

    progress = [
        f for f in conn.enqueued if f.get("event_type") == "model_manager.pull.progress"
    ]
    assert progress, "expected at least one pull.progress keepalive event"
    # Shaped as a gilbert.event and keyed to the originating request id.
    assert all(f["type"] == "gilbert.event" for f in progress)
    assert all(f["data"]["ref"] == "p-prog" for f in progress)
    statuses = [f["data"]["status"] for f in progress]
    assert "pulling manifest" in statuses
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_emits_immediate_keepalive_before_runtime_streams() -> None:
    """The handler pushes one keepalive event BEFORE awaiting the runtime, so a
    slow first Ollama frame can't leave the frontend's pull RPC un-reset (and
    thus spuriously timing out into "Retry") during the manifest-fetch gap.

    Regression for the "pull immediately goes to Retry" report: a runtime that
    never reports progress (e.g. a very slow manifest fetch) must still produce
    at least one keepalive keyed to the request id.
    """

    class _SilentRuntime(_FakeRuntime):
        """Pulls successfully but reports NO progress frames of its own."""

        async def pull_model(self, ref: str, on_progress: Any = None) -> None:
            self.pulled.append(ref)
            # Deliberately never calls on_progress — mimics Ollama not yet
            # streaming while it fetches the manifest.

    svc = await _start(_SilentRuntime(), per_model=None)
    conn = _Conn(0)
    resp = await svc._ws_pull(conn, {"id": "p-imm", "ref": "hf.co/owner/Repo:Q4_K_M"})
    assert resp["type"] == "model_manager.pull.result"

    progress = [
        f for f in conn.enqueued if f.get("event_type") == "model_manager.pull.progress"
    ]
    assert progress, "expected an immediate keepalive even with a silent runtime"
    # The first keepalive is keyed to the originating request id (so the
    # frontend resets THAT pending RPC's deadline) and shaped as a gilbert.event.
    assert progress[0]["type"] == "gilbert.event"
    assert progress[0]["data"]["ref"] == "p-imm"
    assert progress[0]["data"]["status"] == "starting"
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
async def test_pull_surfaces_runtime_failure(caplog: pytest.LogCaptureFixture) -> None:
    runtime = _ExplodingRuntime()
    svc = await _start(runtime, _FakePerModelConfig())
    resp = await svc._ws_pull(_Conn(0), {"id": "p5", "ref": "hf.co/owner/Repo:Q4_K_M"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 502
    assert "pull access denied" in resp["error"]
    await svc.stop()


# --- S9: pre-flight quant validation (the Q8_K_P traceback fix) -----------


@pytest.mark.asyncio
async def test_pull_rejects_unrecognized_quant_without_calling_runtime() -> None:
    """A junk quant tag (``Q8_K_P``) is rejected with a clean 400 BEFORE the
    runtime is touched — no Ollama 400 traceback."""
    runtime = _FakeRuntime()
    svc = await _start(runtime, _FakePerModelConfig())
    resp = await svc._ws_pull(
        _Conn(0),
        {"id": "p8", "repo_id": "owner/Repo-GGUF", "quant": "Q8_K_P"},
    )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 400
    assert "Q8_K_P" in resp["error"]
    assert "Q4_K_M" in resp["error"]  # actionable suggestion
    # The runtime was never driven.
    assert runtime.pulled == []
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_rejects_unrecognized_quant_in_prebuilt_ref() -> None:
    """The quant is extracted from a pre-built ``hf.co/<repo>:<tag>`` ref and
    validated too, not just the ``quant`` field."""
    runtime = _FakeRuntime()
    svc = await _start(runtime, _FakePerModelConfig())
    resp = await svc._ws_pull(
        _Conn(0),
        {"id": "p9", "ref": "hf.co/owner/Repo-GGUF:Q8_K_P"},
    )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 400
    assert runtime.pulled == []
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_accepts_bare_ollama_registry_tag() -> None:
    """An Ollama-library pull is a bare registry tag (``llama3.3:70b``) whose
    suffix is a SIZE, not a quantization scheme. The quant pre-flight must skip
    it (no ``hf.co/`` prefix, no ``repo_id``) — otherwise every Ollama pull would
    be wrongly rejected as an invalid quant."""
    runtime = _FakeRuntime()
    svc = await _start(runtime, _FakePerModelConfig())
    resp = await svc._ws_pull(_Conn(0), {"id": "p-oll", "ref": "llama3.3:70b"})
    assert resp["type"] == "model_manager.pull.result"
    assert runtime.pulled == ["llama3.3:70b"]
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_accepts_standard_quant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recognized quant passes validation and reaches the runtime."""
    runtime = _FakeRuntime()
    svc = await _start(runtime, _FakePerModelConfig())

    async def _ctx(model_id: str, **kw: Any) -> int | None:
        return None

    monkeypatch.setattr(hf_catalog, "fetch_context_window", _ctx)

    resp = await svc._ws_pull(
        _Conn(0),
        {"id": "p10", "repo_id": "owner/Repo-GGUF", "quant": "Q4_K_M"},
    )
    assert resp["type"] == "model_manager.pull.result"
    assert runtime.pulled == ["hf.co/owner/Repo-GGUF:Q4_K_M"]
    await svc.stop()


# --- S9: graceful pull failures (no traceback for expected 4xx) ------------


class _ManifestErrorRuntime(_FakeRuntime):
    async def pull_model(self, ref: str, on_progress: Any = None) -> None:
        raise RuntimeError(
            "Ollama pull failed (400): pull model manifest: 400 not a valid quantization scheme"
        )


class _NotFoundRuntime(_FakeRuntime):
    async def pull_model(self, ref: str, on_progress: Any = None) -> None:
        raise RuntimeError("Ollama pull failed (404): repository not found")


class _UnexpectedRuntime(_FakeRuntime):
    async def pull_model(self, ref: str, on_progress: Any = None) -> None:
        raise RuntimeError("kernel panic: totally unexpected explosion")


@pytest.mark.asyncio
async def test_pull_expected_failure_logs_warning_not_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An expected pull failure (manifest / 4xx / quantization) logs at WARNING
    with no exception traceback, and returns a clean ``gilbert.error``."""
    import logging

    svc = await _start(_ManifestErrorRuntime(), _FakePerModelConfig())
    with caplog.at_level(logging.DEBUG):
        resp = await svc._ws_pull(
            _Conn(0),
            {"id": "p11", "ref": "hf.co/owner/Repo-GGUF:Q4_K_M"},
        )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 502
    # No exception-level record (logger.exception emits ERROR with exc_info).
    assert not any(rec.levelno >= logging.ERROR and rec.exc_info for rec in caplog.records), (
        "expected pull failures must not log a traceback"
    )
    # It did log at warning so operators still see it.
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_not_found_failure_is_graceful(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    svc = await _start(_NotFoundRuntime(), _FakePerModelConfig())
    with caplog.at_level(logging.DEBUG):
        resp = await svc._ws_pull(
            _Conn(0),
            {"id": "p12", "ref": "hf.co/owner/Missing-GGUF:Q4_K_M"},
        )
    assert resp["type"] == "gilbert.error"
    assert not any(rec.exc_info for rec in caplog.records)
    await svc.stop()


@pytest.mark.asyncio
async def test_pull_unexpected_failure_still_logs_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuinely unexpected error keeps the full ``logger.exception`` trace —
    we only suppress tracebacks for *expected* 4xx/manifest/quant failures."""
    import logging

    svc = await _start(_UnexpectedRuntime(), _FakePerModelConfig())
    with caplog.at_level(logging.DEBUG):
        resp = await svc._ws_pull(
            _Conn(0),
            {"id": "p13", "ref": "hf.co/owner/Repo-GGUF:Q4_K_M"},
        )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 502
    # Unexpected → traceback preserved.
    assert any(rec.exc_info for rec in caplog.records)
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
