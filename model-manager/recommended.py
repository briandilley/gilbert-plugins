"""Recommended overlay — Gilbert's curated picks for the HF GGUF catalog.

The successor to the ``ollama`` backend's static curated model list
(``_RECOMMENDED_OVERLAY`` in ``ollama_ai.py``). Where that list keyed on
*Ollama registry tags* (``llama3.3``, ``qwen2.5-coder``), this overlay
keys on **Hugging Face GGUF repo ids** (``bartowski/Llama-3.3-70B-...-GGUF``)
because the manager browses HF directly (PRD gilbert#32, S6 gilbert#38).

A small, hand-vetted set (~10) of well-known, tool-capable instruct models
shipped as GGUF. Membership drives the "Recommended" badge in the Browse
UI and the optional "Recommended only" client-side filter; the
name/description give a friendlier label than the raw repo id.

Kept deliberately tiny and provider-neutral — this is an *overlay*, not a
catalog. The full breadth comes from the Hugging Face Hub API; this just
marks the safe, agent-tested choices on top.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "RecommendedModel",
    "RECOMMENDED_MODELS",
    "recommended_repo_ids",
    "lookup_recommended",
    "is_recommended",
]


@dataclass(frozen=True)
class RecommendedModel:
    """One vetted GGUF repo in the recommended overlay.

    - ``repo_id`` — the Hugging Face repo id (``<owner>/<name>``) exactly as
      the Hub API reports it, so membership checks compare verbatim.
    - ``name`` — a friendly display label for the Browse list.
    - ``description`` — a one-line note on why it's recommended.
    """

    repo_id: str
    name: str
    description: str


# Curated GGUF repos: popular, tool-capable instruct models with known-good
# chat templates. Repo ids are public, well-known Hugging Face repositories
# (no private/personal data). ``bartowski`` and the official ``*-GGUF`` orgs
# are the conventional sources of clean GGUF quant sets.
RECOMMENDED_MODELS: tuple[RecommendedModel, ...] = (
    RecommendedModel(
        repo_id="bartowski/Llama-3.3-70B-Instruct-GGUF",
        name="Llama 3.3 70B Instruct",
        description="Meta's flagship 70B chat model — strong tool use.",
    ),
    RecommendedModel(
        repo_id="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        name="Llama 3.1 8B Instruct",
        description="Compact, capable Llama — a good default on modest hardware.",
    ),
    RecommendedModel(
        repo_id="bartowski/Qwen2.5-7B-Instruct-GGUF",
        name="Qwen 2.5 7B Instruct",
        description="Alibaba's well-rounded 7B instruct model — solid tool use.",
    ),
    RecommendedModel(
        repo_id="bartowski/Qwen2.5-Coder-7B-Instruct-GGUF",
        name="Qwen 2.5 Coder 7B",
        description="Code-specialized Qwen — strong at code generation tasks.",
    ),
    RecommendedModel(
        repo_id="bartowski/Qwen2.5-32B-Instruct-GGUF",
        name="Qwen 2.5 32B Instruct",
        description="Larger Qwen instruct — more capable where VRAM allows.",
    ),
    RecommendedModel(
        repo_id="bartowski/Mistral-7B-Instruct-v0.3-GGUF",
        name="Mistral 7B Instruct v0.3",
        description="Mistral AI's open-weight 7B instruction model.",
    ),
    RecommendedModel(
        repo_id="bartowski/Mistral-Nemo-Instruct-2407-GGUF",
        name="Mistral Nemo 12B Instruct",
        description="Mistral's 12B instruct model, Apache 2.0, strong tool use.",
    ),
    RecommendedModel(
        repo_id="bartowski/Phi-4-GGUF",
        name="Phi-4",
        description="Microsoft's compact reasoning model — punches above its size.",
    ),
    RecommendedModel(
        repo_id="bartowski/gemma-2-9b-it-GGUF",
        name="Gemma 2 9B Instruct",
        description="Google's open-weight Gemma 2 instruct model.",
    ),
    RecommendedModel(
        repo_id="bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        name="DeepSeek R1 Distill Qwen 7B",
        description="Reasoning-focused distill from DeepSeek R1.",
    ),
)

# Built once at import — membership is the hot path (badged per search result).
_BY_REPO_ID: dict[str, RecommendedModel] = {m.repo_id: m for m in RECOMMENDED_MODELS}


def recommended_repo_ids() -> frozenset[str]:
    """Return the set of recommended HF repo ids, for fast badge checks."""
    return frozenset(_BY_REPO_ID)


def lookup_recommended(repo_id: str) -> RecommendedModel | None:
    """Return the overlay entry for ``repo_id``, or ``None`` if not vetted."""
    return _BY_REPO_ID.get(repo_id)


def is_recommended(repo_id: str) -> bool:
    """True iff ``repo_id`` is in the recommended overlay."""
    return repo_id in _BY_REPO_ID
