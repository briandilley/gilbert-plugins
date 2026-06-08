"""Live integration test against the real ``llmfit`` binary.

Runs the actual ``llmfit --json system`` probe end-to-end through
:class:`LlmfitHostResources` and validates the mapping onto
:class:`HostResources` — including a cross-check that the RAM figure agrees
with psutil's (which the built-in ``local`` backend uses), proving the
GiB→bytes scale is consistent across both backends so the downstream fit
policy compares like with like.

Auto-skips when the ``llmfit`` binary isn't on PATH, so the suite stays green
on machines without it. Install with ``uv tool install llmfit``.
"""

from __future__ import annotations

import shutil

import psutil
import pytest
from gilbert_plugin_llmfit.llmfit_host_resources import LlmfitHostResources

from gilbert.interfaces.host_resources import HostResources

pytestmark = pytest.mark.skipif(
    shutil.which("llmfit") is None,
    reason="llmfit binary not on PATH — install with 'uv tool install llmfit'",
)


def test_is_available_reflects_real_binary() -> None:
    assert LlmfitHostResources.is_available() is True


async def test_probe_real_llmfit_returns_valid_host_resources() -> None:
    res = await LlmfitHostResources().probe()
    assert isinstance(res, HostResources)
    assert res.total_ram_bytes > 0
    assert 0 < res.available_ram_bytes <= res.total_ram_bytes
    # A detected GPU always has a name; VRAM is bytes-or-None (unknown),
    # never a fabricated zero.
    for gpu in res.gpus:
        assert gpu.name
        assert gpu.total_vram_bytes is None or gpu.total_vram_bytes > 0


async def test_real_ram_agrees_with_psutil() -> None:
    """The GiB→bytes scale must match psutil (used by the ``local`` backend)
    so both host-resources backends feed the fit policy comparable numbers."""
    res = await LlmfitHostResources().probe()
    psutil_total = psutil.virtual_memory().total
    # Within 5%: llmfit rounds total RAM to two decimal places of GiB.
    assert res.total_ram_bytes == pytest.approx(psutil_total, rel=0.05)
