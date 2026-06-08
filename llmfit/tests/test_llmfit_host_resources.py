"""Unit tests for the llmfit-backed host-resources backend.

The ``llmfit`` binary is never required here — ``_parse_system`` is exercised
as a pure function over representative ``llmfit --json system`` payloads, and
``probe()``'s subprocess is faked. A live test against the real binary lives
in ``test_llmfit_integration.py``.
"""

from __future__ import annotations

import json

import pytest
from gilbert_plugin_llmfit.llmfit_host_resources import (
    _BYTES_PER_GB,
    LlmfitHostResources,
)

from gilbert.interfaces.host_resources import HostResources, HostResourcesBackend

_MOD = "gilbert_plugin_llmfit.llmfit_host_resources"

# A real ``llmfit --json system`` payload from a CPU-only host.
_CPU_ONLY = {
    "system": {
        "available_ram_gb": 8.25,
        "backend": "CPU (x86)",
        "cpu_cores": 16,
        "cpu_name": "11th Gen Intel(R) Core(TM) i9-11900H",
        "gpu_count": 0,
        "gpu_name": None,
        "gpu_vram_gb": None,
        "gpus": [],
        "has_gpu": False,
        "total_ram_gb": 31.13,
        "unified_memory": False,
    }
}


# ── registry / selection metadata ─────────────────────────────────


def test_registered_under_llmfit() -> None:
    assert HostResourcesBackend.registered_backends().get("llmfit") is LlmfitHostResources


def test_priority_outranks_local_floor() -> None:
    """Must outrank the built-in ``local`` backend (priority 0) so the service
    auto-prefers it when the binary is present."""
    assert LlmfitHostResources.priority > 0


def test_is_available_true_when_binary_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_MOD}.shutil.which", lambda _name: "/usr/bin/llmfit")
    assert LlmfitHostResources.is_available() is True


def test_is_available_false_when_binary_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_MOD}.shutil.which", lambda _name: None)
    assert LlmfitHostResources.is_available() is False


# ── _parse_system mapping ─────────────────────────────────────────


def test_parse_cpu_only_maps_ram_and_no_gpu() -> None:
    res = LlmfitHostResources._parse_system(_CPU_ONLY)
    assert isinstance(res, HostResources)
    assert res.total_ram_bytes == int(31.13 * _BYTES_PER_GB)
    assert res.available_ram_bytes == int(8.25 * _BYTES_PER_GB)
    assert res.gpus == ()
    assert not res.has_gpu


def test_parse_single_nvidia_gpu_from_top_level_fields() -> None:
    data = {
        "system": {
            "total_ram_gb": 64.0,
            "available_ram_gb": 40.0,
            "has_gpu": True,
            "gpu_count": 1,
            "gpu_name": "NVIDIA GeForce RTX 4090",
            "gpu_vram_gb": 24.0,
            "gpus": [],
            "unified_memory": False,
        }
    }
    res = LlmfitHostResources._parse_system(data)
    assert len(res.gpus) == 1
    assert res.gpus[0].name == "NVIDIA GeForce RTX 4090"
    assert res.gpus[0].total_vram_bytes == int(24.0 * _BYTES_PER_GB)


def test_parse_multi_gpu_from_gpus_array() -> None:
    data = {
        "system": {
            "total_ram_gb": 256.0,
            "available_ram_gb": 200.0,
            "has_gpu": True,
            "gpu_count": 2,
            "gpu_name": "NVIDIA A100",
            "gpu_vram_gb": 80.0,
            "gpus": [
                {"name": "NVIDIA A100 80GB", "vram_gb": 80.0},
                {"name": "NVIDIA A100 80GB", "vram_gb": 80.0},
            ],
            "unified_memory": False,
        }
    }
    res = LlmfitHostResources._parse_system(data)
    assert len(res.gpus) == 2
    assert [g.name for g in res.gpus] == ["NVIDIA A100 80GB", "NVIDIA A100 80GB"]
    assert all(g.total_vram_bytes == int(80.0 * _BYTES_PER_GB) for g in res.gpus)


def test_parse_apple_unified_memory_gpu_gets_total_ram_as_vram() -> None:
    """Apple Silicon shares system RAM with the GPU and often reports no
    discrete VRAM. Represent the unified pool as the GPU's VRAM so the fit
    policy gives Metal a fits-vram (fast) verdict instead of unknown."""
    data = {
        "system": {
            "total_ram_gb": 32.0,
            "available_ram_gb": 24.0,
            "has_gpu": True,
            "gpu_count": 1,
            "gpu_name": "Apple M3 Max",
            "gpu_vram_gb": None,
            "gpus": [],
            "unified_memory": True,
        }
    }
    res = LlmfitHostResources._parse_system(data)
    assert len(res.gpus) == 1
    assert res.gpus[0].name == "Apple M3 Max"
    assert res.gpus[0].total_vram_bytes == int(32.0 * _BYTES_PER_GB)


def test_parse_discrete_gpu_with_unknown_vram_is_none() -> None:
    """A non-unified GPU whose VRAM llmfit couldn't determine stays None
    ('unknown'), never fabricated to RAM."""
    data = {
        "system": {
            "total_ram_gb": 16.0,
            "available_ram_gb": 8.0,
            "has_gpu": True,
            "gpu_count": 1,
            "gpu_name": "Some Exotic GPU",
            "gpu_vram_gb": None,
            "gpus": [],
            "unified_memory": False,
        }
    }
    res = LlmfitHostResources._parse_system(data)
    assert len(res.gpus) == 1
    assert res.gpus[0].total_vram_bytes is None


def test_parse_missing_ram_raises() -> None:
    """RAM is the one figure we can't fabricate — a payload without it is a
    hard failure, not a silent zero."""
    with pytest.raises((ValueError, RuntimeError, KeyError)):
        LlmfitHostResources._parse_system({"system": {"total_ram_gb": None}})


# ── probe() subprocess wiring (binary faked) ──────────────────────


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


async def test_probe_runs_llmfit_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc(0, json.dumps(_CPU_ONLY).encode())

    monkeypatch.setattr(f"{_MOD}.asyncio.create_subprocess_exec", _fake_exec)
    res = await LlmfitHostResources().probe()
    assert res.total_ram_bytes == int(31.13 * _BYTES_PER_GB)
    assert res.gpus == ()


async def test_probe_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc(1, b"", b"hardware detection failed")

    monkeypatch.setattr(f"{_MOD}.asyncio.create_subprocess_exec", _fake_exec)
    with pytest.raises(RuntimeError):
        await LlmfitHostResources().probe()
