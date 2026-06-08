"""Hardware-fit policy tests — the core test seam for S7 (gilbert#39).

``fit_verdict`` is a pure function, so the policy is exercised directly with
host data + sizes as inputs across every tier: fits-VRAM, fits-RAM,
won't-fit, unknown (remote), unknown (no host). The GPU-with-unknown-VRAM
fall-through and the ``model_best_fit`` aggregation are covered too.
"""

from __future__ import annotations

import pytest
from gilbert_plugin_model_manager.fit import (
    DEFAULT_OVERHEAD_FACTOR,
    FITS_RAM,
    FITS_VRAM,
    UNKNOWN,
    WONT_FIT,
    fit_verdict,
    model_best_fit,
)

from gilbert.interfaces.host_resources import GPUInfo, HostResources

GB = 1024**3


def _host(
    *, ram_avail: int, total_ram: int | None = None, gpus: tuple[GPUInfo, ...] = ()
) -> HostResources:
    return HostResources(
        total_ram_bytes=total_ram if total_ram is not None else ram_avail,
        available_ram_bytes=ram_avail,
        gpus=gpus,
    )


# --- fits-vram ------------------------------------------------------------


def test_fits_vram_when_required_within_gpu_vram() -> None:
    # 4 GB quant × 1.2 = 4.8 GB required, GPU has 24 GB → fully on GPU.
    host = _host(ram_avail=8 * GB, gpus=(GPUInfo(name="RTX 4090", total_vram_bytes=24 * GB),))
    assert fit_verdict(4 * GB, host, remote=False) == FITS_VRAM


def test_fits_vram_picks_any_gpu_that_holds_it() -> None:
    # First GPU too small, second large enough → still fits-vram.
    host = _host(
        ram_avail=8 * GB,
        gpus=(
            GPUInfo(name="small", total_vram_bytes=2 * GB),
            GPUInfo(name="big", total_vram_bytes=48 * GB),
        ),
    )
    assert fit_verdict(20 * GB, host, remote=False) == FITS_VRAM


def test_overhead_factor_applied_to_vram_boundary() -> None:
    # Exactly at the boundary: size × factor == VRAM → fits (<=).
    size = 10 * GB
    vram = int(size * DEFAULT_OVERHEAD_FACTOR)
    host = _host(ram_avail=GB, gpus=(GPUInfo(name="g", total_vram_bytes=vram),))
    assert fit_verdict(size, host, remote=False) == FITS_VRAM
    # One byte over the boundary → no longer fits VRAM (and won't fit the
    # tiny RAM either) → wont-fit, proving overhead is actually applied.
    host_tight = _host(ram_avail=GB, gpus=(GPUInfo(name="g", total_vram_bytes=vram - 1),))
    assert fit_verdict(size, host_tight, remote=False) == WONT_FIT


# --- fits-ram -------------------------------------------------------------


def test_fits_ram_when_no_gpu() -> None:
    # No GPU at all → falls to the RAM check. 4 GB × 1.2 = 4.8 ≤ 16 GB RAM.
    host = _host(ram_avail=16 * GB, gpus=())
    assert fit_verdict(4 * GB, host, remote=False) == FITS_RAM


def test_fits_ram_when_required_exceeds_vram_but_fits_ram() -> None:
    # GPU too small (8 GB), required 12 GB, but 32 GB RAM holds it → slow RAM.
    host = _host(ram_avail=32 * GB, gpus=(GPUInfo(name="g", total_vram_bytes=8 * GB),))
    assert fit_verdict(10 * GB, host, remote=False) == FITS_RAM


def test_gpu_with_unknown_vram_falls_through_to_ram() -> None:
    # A detected GPU whose VRAM is unknown (None) can't satisfy the VRAM
    # check and must NOT crash — it falls through to the RAM check.
    host = _host(ram_avail=16 * GB, gpus=(GPUInfo(name="mystery", total_vram_bytes=None),))
    assert fit_verdict(4 * GB, host, remote=False) == FITS_RAM


# --- wont-fit -------------------------------------------------------------


def test_wont_fit_when_exceeds_both_vram_and_ram() -> None:
    host = _host(ram_avail=8 * GB, gpus=(GPUInfo(name="g", total_vram_bytes=8 * GB),))
    # 70 GB × 1.2 = 84 GB — exceeds both 8 GB VRAM and 8 GB RAM.
    assert fit_verdict(70 * GB, host, remote=False) == WONT_FIT


def test_wont_fit_no_gpu_and_too_big_for_ram() -> None:
    host = _host(ram_avail=8 * GB, gpus=())
    assert fit_verdict(70 * GB, host, remote=False) == WONT_FIT


# --- unknown --------------------------------------------------------------


def test_unknown_when_remote() -> None:
    # Even with ample local-looking host data, a remote Ollama is unknown:
    # the verdict must reflect the box where Ollama runs, not this one.
    host = _host(ram_avail=64 * GB, gpus=(GPUInfo(name="g", total_vram_bytes=48 * GB),))
    assert fit_verdict(4 * GB, host, remote=True) == UNKNOWN


def test_unknown_when_host_is_none() -> None:
    # No host-resources capability available → honestly unknown.
    assert fit_verdict(4 * GB, None, remote=False) == UNKNOWN


def test_remote_takes_precedence_even_with_none_host() -> None:
    assert fit_verdict(4 * GB, None, remote=True) == UNKNOWN


def test_custom_overhead_factor_honoured() -> None:
    # With a 2.0 factor, 5 GB needs 10 GB; a 9 GB GPU no longer fits VRAM.
    host = _host(ram_avail=64 * GB, gpus=(GPUInfo(name="g", total_vram_bytes=9 * GB),))
    assert fit_verdict(5 * GB, host, remote=False, overhead_factor=2.0) == FITS_RAM
    # With the default 1.2 factor it would have fit VRAM (6 ≤ 9).
    assert fit_verdict(5 * GB, host, remote=False) == FITS_VRAM


# --- model_best_fit -------------------------------------------------------


def test_model_best_fit_prefers_vram() -> None:
    assert model_best_fit([WONT_FIT, FITS_RAM, FITS_VRAM]) == FITS_VRAM


def test_model_best_fit_ram_over_wont_fit() -> None:
    assert model_best_fit([WONT_FIT, FITS_RAM, UNKNOWN]) == FITS_RAM


def test_model_best_fit_wont_fit_over_unknown() -> None:
    assert model_best_fit([UNKNOWN, WONT_FIT, UNKNOWN]) == WONT_FIT


def test_model_best_fit_all_unknown() -> None:
    assert model_best_fit([UNKNOWN, UNKNOWN]) == UNKNOWN


def test_model_best_fit_empty_is_unknown() -> None:
    assert model_best_fit([]) == UNKNOWN


def test_model_best_fit_ignores_unrecognized_verdicts() -> None:
    # A stray value ranks no better than unknown, so a real fits-ram wins.
    assert model_best_fit(["nonsense", FITS_RAM]) == FITS_RAM


@pytest.mark.parametrize(
    "verdict",
    [FITS_VRAM, FITS_RAM, WONT_FIT, UNKNOWN],
)
def test_verdict_strings_are_stable_wire_values(verdict: str) -> None:
    # Guard the serialized strings the frontend matches on.
    assert verdict in {"fits-vram", "fits-ram", "wont-fit", "unknown"}
