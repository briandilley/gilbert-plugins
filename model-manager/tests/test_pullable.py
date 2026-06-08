"""Pullable-quant tests (S9, the Q8_K_P traceback fix).

``is_pullable_quant`` / the ``Quant.pullable`` flag are the pure seam: only
quantization tags Ollama actually accepts as ``hf.co/<repo>:<tag>`` pull
references are pullable. A junk/community label (e.g. ``Q8_K_P``) is not, so
the UI can disable the button and the RPC can reject it cleanly *before*
calling Ollama (no 400 traceback).
"""

from __future__ import annotations

import pytest
from gilbert_plugin_model_manager.hf_catalog import (
    OLLAMA_PULLABLE_QUANTS,
    is_pullable_quant,
)


@pytest.mark.parametrize(
    "label",
    [
        "Q4_K_M",
        "q4_k_m",  # case-insensitive
        "Q8_0",
        "Q2_K",
        "Q6_K",
        "F16",
        "BF16",
        "F32",
        "IQ2_XXS",
        "IQ4_XS",
        "IQ4_NL",
    ],
)
def test_standard_quants_are_pullable(label: str) -> None:
    assert is_pullable_quant(label) is True


@pytest.mark.parametrize(
    "label",
    [
        "Q8_K_P",  # the reported junk label
        "Q4_K_XL",
        "Q5_K_XXL",
        "IQ9_BOGUS",
        "totally-made-up",
        "",
        None,
    ],
)
def test_junk_quants_are_not_pullable(label: str | None) -> None:
    assert is_pullable_quant(label) is False


def test_known_set_contains_the_standard_schemes() -> None:
    # Spot-check a representative slice of the canonical llama.cpp / Ollama set.
    for expected in ("Q4_K_M", "Q8_0", "Q6_K", "F16", "BF16", "IQ3_M", "IQ4_NL"):
        assert expected in OLLAMA_PULLABLE_QUANTS
    # The junk one is explicitly absent.
    assert "Q8_K_P" not in OLLAMA_PULLABLE_QUANTS
