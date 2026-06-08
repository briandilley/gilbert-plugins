"""llmfit-backed host-resources probe — multi-vendor GPU detection.

Shells out to the optional ``llmfit`` CLI (``llmfit --json system``) to detect
host hardware. llmfit detects NVIDIA, AMD (``rocm-smi``), Apple Silicon, and
Intel Arc GPUs — plus unified-memory systems — which is broader than the
built-in ``local`` backend's NVIDIA-only ``nvidia-smi`` probe. The richer
snapshot is mapped onto the same :class:`HostResources` shape, so the
model-manager's existing hardware-fit policy consumes it unchanged.

This backend registers at a higher :attr:`priority` than ``local`` and gates
itself on the binary being on ``PATH`` (:meth:`is_available`). The
host-resources service therefore auto-prefers it when ``llmfit`` is installed
and transparently falls back to ``local`` when it isn't.

Scope: only ``llmfit --json system`` is used. llmfit's own fit verdicts and
tokens/sec estimates are computed against its embedded model database and
don't map to the arbitrary Hugging Face GGUF repos Gilbert sizes directly, so
they're intentionally not consumed here — we take only the hardware detection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Iterable
from typing import Any

from gilbert.interfaces.host_resources import (
    GPUInfo,
    HostResources,
    HostResourcesBackend,
)

logger = logging.getLogger(__name__)

_BINARY = "llmfit"
_PROBE_TIMEOUT_S = 10.0

# llmfit reports memory in binary gibibytes (a "32 GiB" module reads as
# ~31.13), which lines up with psutil's byte totals once scaled by 1024**3 —
# keep the two host-resources backends' numbers in the same units so the
# downstream fit policy compares like with like.
_BYTES_PER_GB = 1024**3


def _gb_to_bytes(value: Any) -> int | None:
    """Convert a GiB number from llmfit to bytes, or ``None`` if unusable.

    ``bool`` is rejected explicitly (it's an ``int`` subclass) so a stray
    ``True`` never reads as 1 GiB.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value * _BYTES_PER_GB)


def _first_str(d: dict[str, Any], keys: Iterable[str]) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _first_num(d: dict[str, Any], keys: Iterable[str]) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


class LlmfitHostResources(HostResourcesBackend):
    """Host-resources backend that delegates detection to the ``llmfit`` CLI."""

    backend_name = "llmfit"
    # Outranks the built-in ``local`` floor (priority 0): when the binary is
    # present we prefer llmfit's broader detection; when it's absent
    # ``is_available`` returns False and the service falls back to ``local``.
    priority = 10

    @classmethod
    def is_available(cls) -> bool:
        """True iff the ``llmfit`` binary is on ``PATH``. Cheap, never raises."""
        return shutil.which(_BINARY) is not None

    async def probe(self) -> HostResources:
        data = await self._run_system_json()
        return self._parse_system(data)

    async def _run_system_json(self) -> dict[str, Any]:
        """Run ``llmfit --json system`` and return the parsed JSON.

        Raises :class:`RuntimeError` on any failure (launch error, timeout,
        non-zero exit, unparseable output). The host-resources service has
        already gated selection on :meth:`is_available`, and the model-manager
        degrades a raising probe to "unknown" verdicts — so a transient llmfit
        failure is surfaced clearly rather than masked.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                _BINARY,
                "--json",
                "system",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RuntimeError(f"failed to launch '{_BINARY}': {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S)
        except TimeoutError as exc:
            proc.kill()
            raise RuntimeError(
                f"'{_BINARY} --json system' timed out after {_PROBE_TIMEOUT_S:.0f}s"
            ) from exc

        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip() or f"exit code {proc.returncode}"
            raise RuntimeError(f"'{_BINARY} --json system' failed: {detail}")

        try:
            data = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"'{_BINARY} --json system' returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"'{_BINARY} --json system' returned non-object JSON")
        return data

    @staticmethod
    def _parse_system(data: dict[str, Any]) -> HostResources:
        """Map an ``llmfit --json system`` payload onto :class:`HostResources`.

        RAM is required; a payload without a usable total RAM figure is a hard
        error (we never fabricate the one number we can't infer). GPUs come
        from the ``gpus`` array when present, else the single-GPU top-level
        fields. On unified-memory hosts (Apple Silicon) a GPU with no discrete
        VRAM is given the system RAM as its VRAM, so the fit policy treats
        Metal as fast (fits-vram) rather than unknown.
        """
        system = data.get("system")
        if not isinstance(system, dict):
            raise RuntimeError("llmfit system output missing a 'system' object")

        total = _gb_to_bytes(system.get("total_ram_gb"))
        if total is None:
            raise RuntimeError("llmfit reported no usable total RAM")
        available = _gb_to_bytes(system.get("available_ram_gb"))
        if available is None:
            available = total

        unified = bool(system.get("unified_memory"))
        gpus = LlmfitHostResources._parse_gpus(system, total_ram_bytes=total, unified=unified)
        return HostResources(
            total_ram_bytes=total,
            available_ram_bytes=available,
            gpus=gpus,
        )

    @staticmethod
    def _parse_gpus(
        system: dict[str, Any], *, total_ram_bytes: int, unified: bool
    ) -> tuple[GPUInfo, ...]:
        raw = system.get("gpus")
        pairs: list[tuple[str, int | None]] = []
        if isinstance(raw, list) and raw:
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = _first_str(item, ("name", "gpu_name", "model")) or "GPU"
                vram = _gb_to_bytes(
                    _first_num(item, ("vram_gb", "total_vram_gb", "memory_gb", "gpu_vram_gb"))
                )
                pairs.append((name, vram))
        elif system.get("has_gpu"):
            name = _first_str(system, ("gpu_name", "name")) or "GPU"
            vram = _gb_to_bytes(system.get("gpu_vram_gb"))
            pairs.append((name, vram))

        gpus: list[GPUInfo] = []
        for name, vram in pairs:
            # Unified memory (Apple Silicon): the GPU shares the system RAM
            # pool, so an unreported VRAM means "all of RAM," not "unknown."
            if vram is None and unified:
                vram = total_ram_bytes
            gpus.append(GPUInfo(name=name, total_vram_bytes=vram))
        return tuple(gpus)
