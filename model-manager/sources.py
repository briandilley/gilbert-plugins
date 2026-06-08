"""Model **sources** — the provider-neutral catalog abstraction (S10).

Until S9 the installer browsed exactly one place: the Hugging Face GGUF Hub.
S10 (gilbert-plugins#17) makes the installer **multi-source**: the user first
picks a *source*, then that source's own search/filter affordances appear,
then they find a model and pull it. Adding a third source later is purely
additive — register one more :class:`ModelSource` subclass; the service, the
RPCs, and the SPA discover it automatically.

The abstraction has three small, provider-neutral shapes plus an ABC:

- :class:`SourceDescriptor` — what the SPA renders the source selector from
  (id, label, blurb, and *how* the source is browsed: live ``search`` vs a
  fixed ``curated`` list, plus the noun for an expanded variant — "quantization"
  on HF, "size" on Ollama).
- :class:`SourceModel` — one model row (generalizes the old ``CatalogModel``).
- :class:`SourceVariant` — one pullable variant of a model: an HF *quant* or an
  Ollama registry *size tag* (generalizes the old ``Quant``). Carries the exact
  ``pull_ref`` Ollama needs and a ``size_bytes`` (real for HF, estimated for
  Ollama) so the manager's hardware-fit policy works uniformly across sources.
- :class:`ModelSource` — the ABC each source implements: ``descriptor()`` +
  async ``search()`` + async ``list_variants()``.

Two concrete sources ship today:

- :class:`HuggingFaceSource` — thin wrapper over :mod:`.hf_catalog` (the
  existing live GGUF search / per-quant browse, unchanged in behavior).
- :class:`OllamaLibrarySource` — a **curated** list of well-known Ollama
  registry models (drawn from / extending the families the ``ollama`` backend
  already recommends). Ollama has **no public library search API**, so this is
  deliberately a curated list with a client-side filter — surfaced honestly in
  the UI as "curated", never faked as a full search. Each model expands to its
  common size tags (``llama3.3:70b`` …); the pull ref is the bare registry tag.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from . import hf_catalog, ollama_library

__all__ = [
    "SOURCE_KIND_SEARCH",
    "SOURCE_KIND_CURATED",
    "SourceDescriptor",
    "SourceModel",
    "SourceVariant",
    "ModelSource",
    "HuggingFaceSource",
    "OllamaLibrarySource",
    "all_sources",
    "get_source",
    "HUGGINGFACE_SOURCE_ID",
    "OLLAMA_SOURCE_ID",
]

# How a source is browsed. ``search`` ⇒ a live query against a remote catalog
# (Hugging Face). ``curated`` ⇒ a fixed, hand-vetted list filtered client-side
# (Ollama library — no public search API exists, so we never pretend otherwise).
SOURCE_KIND_SEARCH = "search"
SOURCE_KIND_CURATED = "curated"

HUGGINGFACE_SOURCE_ID = "huggingface"
OLLAMA_SOURCE_ID = "ollama"


@dataclass(frozen=True)
class SourceDescriptor:
    """What the SPA renders the source selector + per-source affordances from.

    - ``id`` — stable source identifier, sent back on every search/variants RPC.
    - ``label`` / ``description`` — friendly text for the selector.
    - ``kind`` — :data:`SOURCE_KIND_SEARCH` (live search) or
      :data:`SOURCE_KIND_CURATED` (fixed list + client-side filter). The UI
      uses this to label a curated source honestly and to skip the "no params
      sort on the Hub" caveats that only apply to HF.
    - ``search_placeholder`` — placeholder text for the search/filter input.
    - ``variant_noun`` — singular noun for an expanded variant ("quantization"
      on HF, "size" on Ollama), used in the expand UI's labels.
    - ``supports_sort`` — whether the source honors the sort dropdown (HF yes,
      curated Ollama no).
    - ``supports_recommended_only`` — whether a "Recommended only" toggle is
      meaningful (HF only — the whole Ollama list is already curated).
    """

    id: str
    label: str
    description: str
    kind: str
    search_placeholder: str
    variant_noun: str
    supports_sort: bool
    supports_recommended_only: bool


@dataclass(frozen=True)
class SourceModel:
    """One model row from a source (generalizes the HF-only ``CatalogModel``).

    - ``id`` — the model identifier within the source (an HF repo id, or an
      Ollama registry name like ``llama3.3``). Passed back to
      :meth:`ModelSource.list_variants`.
    - ``name`` — friendly display name (``None`` ⇒ the SPA falls back to ``id``).
    - ``downloads`` / ``likes`` — popularity signals (0 when the source has no
      such signal, e.g. the curated Ollama list).
    - ``last_modified`` — ISO-8601 string, or ``None``.
    - ``recommended`` — in Gilbert's curated overlay (HF) / always True for the
      curated Ollama list.
    - ``params_b`` — approximate parameter count in billions, or ``None``.
    """

    id: str
    name: str | None
    downloads: int
    likes: int
    last_modified: str | None
    recommended: bool
    params_b: float | None


@dataclass(frozen=True)
class SourceVariant:
    """One pullable variant of a model — an HF quant or an Ollama size tag.

    - ``variant_id`` — stable per-row key (the HF gguf filename, or the Ollama
      registry tag).
    - ``label`` — what the UI shows (the quant label ``Q4_K_M`` / the size tag
      ``70b``).
    - ``pull_ref`` — the **exact** reference handed to ``model_manager.pull``:
      ``hf.co/<repo>:<quant>`` for HF, or the bare registry tag
      (``llama3.3:70b``) for Ollama.
    - ``size_bytes`` — on-disk size. **Exact** for HF (from the Hub blob size),
      **estimated** for Ollama (params × a Q4_K_M bytes-per-param factor — the
      registry has no per-tag size API), or ``None`` when unknown. Feeds the
      hardware-fit policy uniformly across sources.
    - ``size_estimated`` — True when ``size_bytes`` is an estimate (Ollama), so
      the UI can mark the fit verdict as approximate rather than exact.
    - ``pullable`` — whether the runtime accepts ``pull_ref`` (HF: a recognized
      quant scheme; Ollama: always True for a curated registry tag).
    - ``params_b`` — parameter count for this specific variant when it differs
      from the model's (an Ollama size tag pins the size), else ``None``.
    """

    variant_id: str
    label: str
    pull_ref: str
    size_bytes: int | None
    size_estimated: bool
    pullable: bool
    params_b: float | None


class ModelSource(ABC):
    """A browsable place to find local models to pull.

    Each source maps its own catalog onto the provider-neutral
    :class:`SourceModel` / :class:`SourceVariant` shapes so the manager service,
    the WS RPCs, and the SPA stay source-agnostic. The pull itself always goes
    through ``model_manager.pull`` with the variant's ``pull_ref``.
    """

    @abstractmethod
    def descriptor(self) -> SourceDescriptor:
        """Return the static metadata the SPA renders this source from."""

    @abstractmethod
    async def search(
        self,
        query: str,
        sort: str,
        limit: int,
        *,
        recommended_only: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> list[SourceModel]:
        """Return matching models. A ``search`` source queries a remote catalog;
        a ``curated`` source filters its fixed list by ``query`` client-side."""

    @abstractmethod
    async def list_variants(
        self,
        model_id: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[SourceVariant]:
        """Return the pullable variants (quants / size tags) of ``model_id``."""


class HuggingFaceSource(ModelSource):
    """The Hugging Face GGUF Hub — live search, per-quant browse (unchanged).

    A thin adapter over :mod:`.hf_catalog`: ``search`` maps each
    ``CatalogModel`` and ``list_variants`` maps each ``Quant`` into the
    provider-neutral shapes, building the ``hf.co/<repo>:<quant>`` pull ref.
    """

    def descriptor(self) -> SourceDescriptor:
        return SourceDescriptor(
            id=HUGGINGFACE_SOURCE_ID,
            label="Hugging Face",
            description="Search the Hugging Face Hub for GGUF models.",
            kind=SOURCE_KIND_SEARCH,
            search_placeholder="Search GGUF models (e.g. qwen, llama)…",
            variant_noun="quantization",
            supports_sort=True,
            supports_recommended_only=True,
        )

    async def search(
        self,
        query: str,
        sort: str,
        limit: int,
        *,
        recommended_only: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> list[SourceModel]:
        models = await hf_catalog.search(
            query, sort, limit, recommended_only=recommended_only, client=client
        )
        return [
            SourceModel(
                id=m.id,
                name=None,
                downloads=m.downloads,
                likes=m.likes,
                last_modified=m.last_modified,
                recommended=m.recommended,
                params_b=m.params_b,
            )
            for m in models
        ]

    async def list_variants(
        self,
        model_id: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[SourceVariant]:
        quants = await hf_catalog.list_quants(model_id, client=client)
        variants: list[SourceVariant] = []
        for q in quants:
            label = q.quant_label or q.filename
            # Only buildable into a pull ref when there's a real quant label;
            # a labelless gguf is non-pullable (mirrors the old PullButton gate).
            pull_ref = f"hf.co/{model_id}:{q.quant_label}" if q.quant_label else ""
            variants.append(
                SourceVariant(
                    variant_id=q.filename,
                    label=label,
                    pull_ref=pull_ref,
                    size_bytes=q.size_bytes,
                    size_estimated=False,
                    pullable=q.pullable,
                    params_b=None,
                )
            )
        return variants


class OllamaLibrarySource(ModelSource):
    """The Ollama registry — a **curated** list, filtered client-side.

    Ollama exposes no public search API for its library, so this source is an
    honestly-curated list (drawn from / extending the families the ``ollama``
    backend recommends), not a fabricated search. ``search`` filters that list
    by substring; ``list_variants`` returns each model's common size tags with
    an **estimated** GGUF size (Ollama serves no per-tag size API) so the
    hardware-fit policy still works — flagged ``size_estimated=True`` so the UI
    is upfront about it. The pull ref is the bare registry tag.
    """

    def descriptor(self) -> SourceDescriptor:
        return SourceDescriptor(
            id=OLLAMA_SOURCE_ID,
            label="Ollama library",
            description=(
                "A curated set of popular models from the Ollama registry. "
                "Ollama has no public search API, so this is a hand-picked list, "
                "not a full catalog search."
            ),
            kind=SOURCE_KIND_CURATED,
            search_placeholder="Filter the curated list (e.g. llama, coder)…",
            variant_noun="size",
            supports_sort=False,
            supports_recommended_only=False,
        )

    async def search(
        self,
        query: str,
        sort: str,
        limit: int,
        *,
        recommended_only: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> list[SourceModel]:
        # Pure, client-side filter over the curated registry list — no network.
        matches = ollama_library.search(query, limit)
        return [
            SourceModel(
                id=m.name,
                name=m.display_name,
                downloads=0,
                likes=0,
                last_modified=None,
                recommended=True,
                params_b=ollama_library.largest_params_b(m),
            )
            for m in matches
        ]

    async def list_variants(
        self,
        model_id: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[SourceVariant]:
        tags = ollama_library.list_tags(model_id)
        return [
            SourceVariant(
                variant_id=t.pull_ref,
                label=t.label,
                pull_ref=t.pull_ref,
                size_bytes=t.estimated_size_bytes,
                size_estimated=True,
                pullable=True,
                params_b=t.params_b,
            )
            for t in tags
        ]


# Registered sources, in selector order. Adding a third source is a one-line
# append here plus its ``ModelSource`` subclass — the service/RPCs/SPA discover
# it automatically.
_SOURCES: tuple[ModelSource, ...] = (
    HuggingFaceSource(),
    OllamaLibrarySource(),
)

_BY_ID: dict[str, ModelSource] = {s.descriptor().id: s for s in _SOURCES}


def all_sources() -> tuple[ModelSource, ...]:
    """Return every registered source, in selector order."""
    return _SOURCES


def get_source(source_id: str) -> ModelSource | None:
    """Return the source with ``source_id``, or ``None`` if unknown."""
    return _BY_ID.get(source_id)
