"""Curated Ollama-registry catalog (S10).

Ollama's model **library** (https://ollama.com/library) has **no public
search/JSON API** — you can't query it the way the Hugging Face Hub API lets
you query GGUF repos. So rather than fake a search, this module ships a small,
hand-vetted list of well-known registry models (drawn from / extending the
families the ``ollama`` backend already recommends in its
``_RECOMMENDED_OVERLAY``), each with its common **size tags** (``70b``,
``7b`` …). The installer browses this list with a client-side substring filter
and surfaces, honestly, that it's a curated list — not a full catalog search.

Two things callers need that the registry won't give us over an API:

- **A pull ref.** ``ollama pull <name>:<tag>`` takes a bare registry tag —
  ``llama3.3:70b`` — which is exactly :attr:`OllamaTag.pull_ref`.
- **A size, for the hardware-fit verdict.** The registry serves no per-tag
  size API, so we *estimate* the on-disk GGUF size from the parameter count and
  Ollama's default quantization (Q4_K_M ≈ 0.6 bytes/param incl. overhead). It's
  an estimate, flagged as such upstream (``size_estimated=True``) so the UI
  doesn't present an estimated fit as exact.

Everything here is pure and provider-neutral — no network, no Ollama coupling.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "QUANT_BYTES_PER_PARAM",
    "OllamaTag",
    "OllamaModel",
    "OLLAMA_MODELS",
    "search",
    "list_tags",
    "get_model",
    "largest_params_b",
    "estimate_size_bytes",
]

# Effective on-disk bytes per parameter for Ollama's default Q4_K_M GGUF. A
# 4-bit k-quant is ~4.5 bits/weight (≈0.56 B/param) plus model overhead; 0.6
# gives a slightly conservative whole-file estimate. Used ONLY to estimate a
# size for the hardware-fit verdict — never presented as an exact figure.
QUANT_BYTES_PER_PARAM = 0.6 * 1_000_000_000  # bytes per *billion* params

_BYTES_PER_GB = 1_000_000_000


@dataclass(frozen=True)
class OllamaTag:
    """One pullable size tag of a registry model.

    - ``label`` — the size suffix the UI shows (``70b``, ``7b``, ``latest``).
    - ``pull_ref`` — the exact ``ollama pull`` reference (``llama3.3:70b``).
    - ``params_b`` — parameter count in billions this tag pins (drives the size
      estimate + fit), or ``None`` for a meta tag like ``latest``.
    - ``estimated_size_bytes`` — estimated on-disk GGUF size, or ``None`` when
      ``params_b`` is unknown.
    """

    label: str
    pull_ref: str
    params_b: float | None
    estimated_size_bytes: int | None


@dataclass(frozen=True)
class OllamaModel:
    """One curated registry model and its common size tags.

    - ``name`` — the registry name / base tag (``llama3.3``).
    - ``display_name`` — friendly label for the list row.
    - ``description`` — one-line note.
    - ``sizes_b`` — the parameter sizes (in billions) published for this model,
      in registry-tag order. ``()`` ⇒ only a ``latest`` tag is offered.
    """

    name: str
    display_name: str
    description: str
    sizes_b: tuple[float, ...]


def estimate_size_bytes(params_b: float | None) -> int | None:
    """Estimate the default-quant (Q4_K_M) on-disk size for ``params_b``.

    Returns ``None`` when the param count is unknown. The estimate is
    deliberately rough — it exists only to give the hardware-fit policy
    something to classify, and is always flagged as estimated to the UI.
    """
    if params_b is None or params_b <= 0:
        return None
    return int(params_b * QUANT_BYTES_PER_PARAM)


def _size_label(params_b: float) -> str:
    """Render a param size as a registry-style tag (``70b`` / ``3.8b``)."""
    if params_b.is_integer():
        return f"{int(params_b)}b"
    # Trim a trailing zero (3.80 → 3.8) but keep one decimal place otherwise.
    return f"{params_b:g}b"


# The curated registry list. Names are real Ollama library models; ``sizes_b``
# are the commonly-published size tags. Kept small and well-known — this is an
# overlay of safe, agent-tested choices, not the whole registry.
OLLAMA_MODELS: tuple[OllamaModel, ...] = (
    OllamaModel(
        name="llama3.3",
        display_name="Llama 3.3",
        description="Meta's flagship 70B chat model — strong tool use.",
        sizes_b=(70.0,),
    ),
    OllamaModel(
        name="llama3.2",
        display_name="Llama 3.2",
        description="Meta's compact Llama 3.2 line (1B / 3B).",
        sizes_b=(1.0, 3.0),
    ),
    OllamaModel(
        name="llama3.1",
        display_name="Llama 3.1",
        description="Meta's Llama 3.1 instruct line (8B / 70B / 405B).",
        sizes_b=(8.0, 70.0, 405.0),
    ),
    OllamaModel(
        name="qwen2.5",
        display_name="Qwen 2.5",
        description="Alibaba's well-rounded instruct line (0.5B–72B).",
        sizes_b=(0.5, 1.5, 3.0, 7.0, 14.0, 32.0, 72.0),
    ),
    OllamaModel(
        name="qwen2.5-coder",
        display_name="Qwen 2.5 Coder",
        description="Code-specialized Qwen — strong at code generation.",
        sizes_b=(0.5, 1.5, 3.0, 7.0, 14.0, 32.0),
    ),
    OllamaModel(
        name="gemma3",
        display_name="Gemma 3",
        description="Google's open-weight Gemma 3 line (1B–27B).",
        sizes_b=(1.0, 4.0, 12.0, 27.0),
    ),
    OllamaModel(
        name="phi4",
        display_name="Phi-4",
        description="Microsoft's compact reasoning model (~14B).",
        sizes_b=(14.0,),
    ),
    OllamaModel(
        name="mistral",
        display_name="Mistral 7B",
        description="Mistral AI's open-weight 7B instruction model.",
        sizes_b=(7.0,),
    ),
    OllamaModel(
        name="mistral-nemo",
        display_name="Mistral Nemo 12B",
        description="Mistral's 12B model, Apache 2.0 — strong tool use.",
        sizes_b=(12.0,),
    ),
    OllamaModel(
        name="deepseek-r1",
        display_name="DeepSeek R1",
        description="Reasoning-focused open-weight line (1.5B–70B distills).",
        sizes_b=(1.5, 7.0, 8.0, 14.0, 32.0, 70.0),
    ),
)

_BY_NAME: dict[str, OllamaModel] = {m.name: m for m in OLLAMA_MODELS}


def get_model(name: str) -> OllamaModel | None:
    """Return the curated model with registry ``name``, or ``None``."""
    return _BY_NAME.get(name)


def largest_params_b(model: OllamaModel) -> float | None:
    """The model's largest published size (for the list row's param chip)."""
    return max(model.sizes_b) if model.sizes_b else None


def search(query: str, limit: int) -> list[OllamaModel]:
    """Filter the curated list by a case-insensitive substring of name / label.

    An empty query returns the whole list (truncated to ``limit``). Matching is
    purely client-side — there is no Ollama search API to call.
    """
    q = query.strip().lower()
    if not q:
        matches = list(OLLAMA_MODELS)
    else:
        matches = [
            m
            for m in OLLAMA_MODELS
            if q in m.name.lower()
            or q in m.display_name.lower()
            or q in m.description.lower()
        ]
    return matches[: max(int(limit), 0)] if limit else matches


def list_tags(name: str) -> list[OllamaTag]:
    """Return the pullable size tags for registry model ``name``.

    Each published size becomes a ``<name>:<size>b`` tag with an estimated
    size; a model with no published sizes offers a single ``latest`` tag
    (unknown params ⇒ unknown size ⇒ "unknown" fit). Unknown model ⇒ empty.
    """
    model = _BY_NAME.get(name)
    if model is None:
        return []
    if not model.sizes_b:
        return [
            OllamaTag(
                label="latest",
                pull_ref=name,
                params_b=None,
                estimated_size_bytes=None,
            )
        ]
    tags: list[OllamaTag] = []
    for size in model.sizes_b:
        label = _size_label(size)
        tags.append(
            OllamaTag(
                label=label,
                pull_ref=f"{name}:{label}",
                params_b=size,
                estimated_size_bytes=estimate_size_bytes(size),
            )
        )
    return tags
