"""Hardware-fit policy — does a model quant run on *this* host, and how fast?

The manager's **policy**, deliberately kept out of core (ADR-0020): core's
``HostResourcesProvider`` reports raw hardware (RAM / GPU / VRAM) and never a
runnability verdict. This module turns that raw data plus a quant's on-disk
size into one of four verdicts — *fits-VRAM* (fast, fully on GPU),
*fits-RAM* (slow, CPU or partial offload), *won't-fit*, or *unknown*
(remote Ollama, or no host data) — PRD gilbert#32, S7 gilbert#39.

A **pure function** by design so the policy is the test seam: feed it host
data + a size and assert the tier, no service or network needed. The
overhead factor and verdict strings live here as named constants so the
policy can evolve (a richer KV-cache / context estimate, new tiers) without
touching the ``host_resources`` or ``local_model_runtime`` interfaces.
"""

from __future__ import annotations

from gilbert.interfaces.host_resources import HostResources

__all__ = [
    "FITS_VRAM",
    "FITS_RAM",
    "WONT_FIT",
    "UNKNOWN",
    "DEFAULT_OVERHEAD_FACTOR",
    "fit_verdict",
    "model_best_fit",
]

# --- Verdict strings ------------------------------------------------------
# Stable wire values: serialized verbatim onto each quant's ``fit`` field and
# matched by the SPA's badge component. Keep in sync with frontend/types.ts.
FITS_VRAM = "fits-vram"
"""Required memory fits in a single GPU's VRAM — runs fully on GPU (fast)."""
FITS_RAM = "fits-ram"
"""Doesn't fit VRAM but fits available system RAM — CPU / partial offload (slow)."""
WONT_FIT = "wont-fit"
"""Exceeds both VRAM and available RAM — won't load on this host."""
UNKNOWN = "unknown"
"""Can't be determined — remote Ollama, or no host-resources data."""

# --- Policy constants -----------------------------------------------------
# A loaded model needs more memory than its file size: weights plus the KV
# cache / context activations and runtime overhead. We approximate that with
# a flat multiplier rather than a per-model context estimate — good enough to
# keep a "fits" verdict honest (a quant marked fits actually loads), and
# cheap to evolve into a context-aware estimate later without an interface
# change. 1.2 ≈ a 20% headroom over raw weights.
DEFAULT_OVERHEAD_FACTOR = 1.2

# Best-to-worst ordering of the verdicts, for aggregating a model's quants
# into a single best-case verdict (the model is "compatible" if its best
# quant is). Lower rank == better.
_VERDICT_RANK: dict[str, int] = {
    FITS_VRAM: 0,
    FITS_RAM: 1,
    WONT_FIT: 2,
    UNKNOWN: 3,
}


def fit_verdict(
    size_bytes: int,
    host: HostResources | None,
    *,
    remote: bool,
    overhead_factor: float = DEFAULT_OVERHEAD_FACTOR,
) -> str:
    """Classify how a quant of ``size_bytes`` fits on the host.

    Returns one of :data:`FITS_VRAM` / :data:`FITS_RAM` / :data:`WONT_FIT` /
    :data:`UNKNOWN`.

    - ``remote=True`` or ``host is None`` → :data:`UNKNOWN`. Fit reflects the
      box where Ollama actually runs; we only probe localhost, so a remote
      ``base_url`` (or absent host-resources capability) is honestly unknown
      rather than a fabricated verdict.
    - Otherwise ``required = size_bytes * overhead_factor`` (weights + KV
      cache / context headroom). If any GPU reports a known
      ``total_vram_bytes`` that holds ``required`` → :data:`FITS_VRAM` (fast).
      Else if ``required`` fits ``host.available_ram_bytes`` →
      :data:`FITS_RAM` (slow, CPU / partial offload). Else :data:`WONT_FIT`.

    A GPU whose ``total_vram_bytes is None`` (unknown VRAM) simply can't
    satisfy the VRAM check and falls through to the RAM check — never a crash.
    """
    if remote or host is None:
        return UNKNOWN

    required = size_bytes * overhead_factor

    # Any single GPU with enough *known* VRAM → fully on GPU (fast). A GPU
    # with unknown VRAM (None) can't be checked, so it's skipped — the model
    # may still fit in RAM below.
    for gpu in host.gpus:
        if gpu.total_vram_bytes is not None and required <= gpu.total_vram_bytes:
            return FITS_VRAM

    # RAM is always known (psutil is a hard dependency on the host probe).
    if required <= host.available_ram_bytes:
        return FITS_RAM

    return WONT_FIT


def model_best_fit(quant_verdicts: list[str]) -> str:
    """Reduce a model's per-quant verdicts to its single best-case verdict.

    A model is as compatible as its best-fitting quant: fits-VRAM beats
    fits-RAM beats won't-fit beats unknown. Used for list-level filtering —
    a model is hidden by the "Compatible" toggle only when even its best
    loaded quant won't fit. An empty list (no quants loaded yet) →
    :data:`UNKNOWN`.
    """
    best = UNKNOWN
    best_rank = _VERDICT_RANK[UNKNOWN]
    for verdict in quant_verdicts:
        rank = _VERDICT_RANK.get(verdict, _VERDICT_RANK[UNKNOWN])
        if rank < best_rank:
            best = verdict
            best_rank = rank
    return best
