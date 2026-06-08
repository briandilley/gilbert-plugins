"""Host-fit RPC tests — ``catalog.quants`` fit augmentation + ``host.resources``.

S7 (gilbert#39) wires the pure fit policy into two service seams:

- ``catalog.quants`` attaches a per-quant ``fit`` verdict, computed from the
  ``host_resources`` capability snapshot and whether the runtime's
  ``base_url`` is local vs remote.
- ``host.resources`` returns a host summary (RAM / GPUs / remote flag) so the
  SPA can explain an "unknown" verdict.

The ``host_resources`` capability and the runtime are faked; the fit policy
itself is unit-tested in ``test_fit.py``. These assert the wiring: the right
host snapshot reaches the policy, remote-ness is derived from the base_url,
and the serialized shapes are stable.
"""

from __future__ import annotations

from typing import Any

import pytest
from gilbert_plugin_model_manager import hf_catalog
from gilbert_plugin_model_manager.hf_catalog import Quant
from gilbert_plugin_model_manager.model_manager_service import ModelManagerService

from gilbert.interfaces.host_resources import GPUInfo, HostResources

GB = 1024**3


class _FakeRuntime:
    """Duck-typed ``LocalModelRuntimeProvider`` with a configurable base_url."""

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self._base_url = base_url

    async def list_models(self) -> list[Any]:  # pragma: no cover - unused here
        return []

    async def pull_model(self, ref: str) -> None:  # pragma: no cover - unused
        return None

    async def delete_model(self, tag: str) -> None:  # pragma: no cover - unused
        return None

    def base_url(self) -> str:
        return self._base_url


class _FakeHostResources:
    """Duck-typed ``HostResourcesProvider`` returning a scripted snapshot.

    ``snapshot=None`` simulates the provider raising (best-effort: the
    service must degrade to "unknown", not crash).
    """

    def __init__(self, snapshot: HostResources | None) -> None:
        self._snapshot = snapshot
        self.calls = 0

    async def get_host_resources(self) -> HostResources:
        self.calls += 1
        if self._snapshot is None:
            raise RuntimeError("probe failed")
        return self._snapshot


class _FakeResolver:
    def __init__(self, runtime: Any, host: Any = None) -> None:
        self._runtime = runtime
        self._host = host

    def get_capability(self, name: str) -> Any:
        if name == "local_model_runtime":
            return self._runtime
        if name == "host_resources":
            return self._host
        return None

    def require_capability(self, name: str) -> Any:
        if name == "local_model_runtime":
            return self._runtime
        raise KeyError(name)

    def get_all(self, name: str) -> list[Any]:  # pragma: no cover - unused
        return []


class _Conn:
    def __init__(self, user_level: int = 0) -> None:
        self.user_level = user_level


def _local_host() -> HostResources:
    return HostResources(
        total_ram_bytes=32 * GB,
        available_ram_bytes=16 * GB,
        gpus=(GPUInfo(name="RTX 4090", total_vram_bytes=24 * GB),),
    )


async def _start(runtime: Any, host: Any = None) -> ModelManagerService:
    svc = ModelManagerService()
    await svc.start(_FakeResolver(runtime, host))
    return svc


def _fake_quants(*quants: Quant) -> Any:
    async def _impl(model_id: str, **kw: Any) -> list[Quant]:
        return list(quants)

    return _impl


# --- catalog.quants fit augmentation --------------------------------------


@pytest.mark.asyncio
async def test_quants_attach_fit_verdicts_local(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHostResources(_local_host())
    svc = await _start(_FakeRuntime("http://localhost:11434"), host)
    monkeypatch.setattr(
        hf_catalog,
        "list_quants",
        _fake_quants(
            Quant(filename="m-Q4_K_M.gguf", quant_label="Q4_K_M", size_bytes=4 * GB),
            Quant(filename="m-Q8_0.gguf", quant_label="Q8_0", size_bytes=20 * GB),
            Quant(filename="m-F16.gguf", quant_label="F16", size_bytes=70 * GB),
            Quant(filename="m-nosize.gguf", quant_label=None, size_bytes=None),
        ),
    )

    resp = await svc._ws_catalog_quants(_Conn(0), {"id": "q", "model_id": "owner/Repo-GGUF"})
    by_file = {q["filename"]: q for q in resp["quants"]}
    # 4 GB × 1.2 = 4.8 ≤ 24 GB VRAM → fast.
    assert by_file["m-Q4_K_M.gguf"]["fit"] == "fits-vram"
    # 20 GB × 1.2 = 24 GB == 24 GB VRAM (<= boundary) → still fits VRAM.
    assert by_file["m-Q8_0.gguf"]["fit"] == "fits-vram"
    # 70 GB × 1.2 = 84 GB > 24 VRAM and > 16 RAM → won't fit.
    assert by_file["m-F16.gguf"]["fit"] == "wont-fit"
    # Unknown size → can't compute → unknown.
    assert by_file["m-nosize.gguf"]["fit"] == "unknown"
    # The existing fields are still present.
    assert by_file["m-Q4_K_M.gguf"]["quant_label"] == "Q4_K_M"
    assert by_file["m-Q4_K_M.gguf"]["size_bytes"] == 4 * GB
    await svc.stop()


@pytest.mark.asyncio
async def test_quants_unknown_when_remote_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHostResources(_local_host())
    # base_url points at a remote box → every verdict is unknown regardless
    # of the (local) host snapshot.
    svc = await _start(_FakeRuntime("http://192.168.1.50:11434"), host)
    monkeypatch.setattr(
        hf_catalog,
        "list_quants",
        _fake_quants(Quant(filename="m-Q4_K_M.gguf", quant_label="Q4_K_M", size_bytes=4 * GB)),
    )
    resp = await svc._ws_catalog_quants(_Conn(0), {"id": "q", "model_id": "owner/Repo"})
    assert resp["quants"][0]["fit"] == "unknown"
    await svc.stop()


@pytest.mark.asyncio
async def test_quants_unknown_when_no_host_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    # host_resources capability absent (None) → unknown, no crash.
    svc = await _start(_FakeRuntime("http://localhost:11434"), host=None)
    monkeypatch.setattr(
        hf_catalog,
        "list_quants",
        _fake_quants(Quant(filename="m-Q4_K_M.gguf", quant_label="Q4_K_M", size_bytes=4 * GB)),
    )
    resp = await svc._ws_catalog_quants(_Conn(0), {"id": "q", "model_id": "owner/Repo"})
    assert resp["quants"][0]["fit"] == "unknown"
    await svc.stop()


@pytest.mark.asyncio
async def test_quants_unknown_when_host_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Provider present but probe raises → degrade to unknown, no crash.
    host = _FakeHostResources(None)
    svc = await _start(_FakeRuntime("http://localhost:11434"), host)
    monkeypatch.setattr(
        hf_catalog,
        "list_quants",
        _fake_quants(Quant(filename="m-Q4_K_M.gguf", quant_label="Q4_K_M", size_bytes=4 * GB)),
    )
    resp = await svc._ws_catalog_quants(_Conn(0), {"id": "q", "model_id": "owner/Repo"})
    assert resp["quants"][0]["fit"] == "unknown"
    await svc.stop()


@pytest.mark.asyncio
async def test_quants_fetches_host_snapshot_once_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHostResources(_local_host())
    svc = await _start(_FakeRuntime("http://localhost:11434"), host)
    monkeypatch.setattr(
        hf_catalog,
        "list_quants",
        _fake_quants(
            Quant(filename="a.gguf", quant_label="Q4_K_M", size_bytes=4 * GB),
            Quant(filename="b.gguf", quant_label="Q8_0", size_bytes=8 * GB),
        ),
    )
    await svc._ws_catalog_quants(_Conn(0), {"id": "q", "model_id": "owner/Repo"})
    # One snapshot for the whole call, not one per quant.
    assert host.calls == 1
    await svc.stop()


# --- host.resources RPC ---------------------------------------------------


@pytest.mark.asyncio
async def test_host_resources_rpc_local_shape() -> None:
    host = _FakeHostResources(_local_host())
    svc = await _start(_FakeRuntime("http://localhost:11434"), host)
    resp = await svc._ws_host_resources(_Conn(0), {"id": "h"})
    assert resp["type"] == "model_manager.host.resources.result"
    assert resp["ref"] == "h"
    assert resp["remote"] is False
    assert resp["total_ram_bytes"] == 32 * GB
    assert resp["available_ram_bytes"] == 16 * GB
    assert resp["gpus"] == [{"name": "RTX 4090", "total_vram_bytes": 24 * GB}]
    await svc.stop()


@pytest.mark.asyncio
async def test_host_resources_rpc_remote_flag() -> None:
    host = _FakeHostResources(_local_host())
    svc = await _start(_FakeRuntime("http://gpu-box.lan:11434"), host)
    resp = await svc._ws_host_resources(_Conn(0), {"id": "h"})
    assert resp["remote"] is True
    await svc.stop()


@pytest.mark.asyncio
async def test_host_resources_rpc_no_capability() -> None:
    svc = await _start(_FakeRuntime("http://localhost:11434"), host=None)
    resp = await svc._ws_host_resources(_Conn(0), {"id": "h"})
    assert resp["type"] == "model_manager.host.resources.result"
    # No host data: RAM null, no gpus. Still a valid (unknown) summary.
    assert resp["total_ram_bytes"] is None
    assert resp["available_ram_bytes"] is None
    assert resp["gpus"] == []
    assert resp["remote"] is False
    await svc.stop()


@pytest.mark.asyncio
async def test_host_resources_rpc_gpu_unknown_vram() -> None:
    snap = HostResources(
        total_ram_bytes=8 * GB,
        available_ram_bytes=4 * GB,
        gpus=(GPUInfo(name="iGPU", total_vram_bytes=None),),
    )
    svc = await _start(_FakeRuntime("http://127.0.0.1:11434"), _FakeHostResources(snap))
    resp = await svc._ws_host_resources(_Conn(0), {"id": "h"})
    assert resp["gpus"] == [{"name": "iGPU", "total_vram_bytes": None}]
    assert resp["remote"] is False
    await svc.stop()


@pytest.mark.asyncio
async def test_host_resources_rpc_admin_gated() -> None:
    svc = await _start(_FakeRuntime("http://localhost:11434"), _FakeHostResources(_local_host()))
    resp = await svc._ws_host_resources(_Conn(1), {"id": "h"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403
    await svc.stop()


@pytest.mark.asyncio
async def test_host_resources_handler_registered() -> None:
    handlers = ModelManagerService().get_ws_handlers()
    assert "model_manager.host.resources" in handlers


def test_service_declares_host_resources_optional() -> None:
    info = ModelManagerService().service_info()
    assert "host_resources" in info.optional


# --- remote detection edge cases ------------------------------------------


@pytest.mark.parametrize(
    "base_url,expect_remote",
    [
        ("http://localhost:11434", False),
        ("http://127.0.0.1:11434", False),
        ("http://[::1]:11434", False),
        ("http://0.0.0.0:11434", False),
        ("", False),
        ("http://192.168.1.5:11434", True),
        ("https://ollama.example.com", True),
        ("http://gpu-box:11434", True),
    ],
)
@pytest.mark.asyncio
async def test_remote_detection(base_url: str, expect_remote: bool) -> None:
    svc = await _start(_FakeRuntime(base_url), _FakeHostResources(_local_host()))
    resp = await svc._ws_host_resources(_Conn(0), {"id": "h"})
    assert resp["remote"] is expect_remote
    await svc.stop()
