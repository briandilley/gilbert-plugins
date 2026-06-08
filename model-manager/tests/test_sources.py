"""Tests for the model-source abstraction (S10).

Covers the registry (``all_sources`` / ``get_source``), the descriptors the SPA
renders the selector from, and that each concrete source maps its catalog onto
the provider-neutral ``SourceModel`` / ``SourceVariant`` shapes — including the
right pull refs (``hf.co/...`` for HF, bare registry tag for Ollama).
"""

from __future__ import annotations

from typing import Any

import pytest
from gilbert_plugin_model_manager import hf_catalog, sources


def test_registry_has_huggingface_and_ollama_in_order() -> None:
    descs = [s.descriptor() for s in sources.all_sources()]
    ids = [d.id for d in descs]
    assert ids == [sources.HUGGINGFACE_SOURCE_ID, sources.OLLAMA_SOURCE_ID]


def test_get_source_known_and_unknown() -> None:
    assert sources.get_source(sources.HUGGINGFACE_SOURCE_ID) is not None
    assert sources.get_source(sources.OLLAMA_SOURCE_ID) is not None
    assert sources.get_source("nope") is None


def test_hf_descriptor_is_a_live_search_source() -> None:
    d = sources.get_source(sources.HUGGINGFACE_SOURCE_ID).descriptor()
    assert d.kind == sources.SOURCE_KIND_SEARCH
    assert d.supports_sort is True
    assert d.supports_recommended_only is True
    assert d.variant_noun == "quantization"


def test_ollama_descriptor_is_a_curated_source() -> None:
    d = sources.get_source(sources.OLLAMA_SOURCE_ID).descriptor()
    assert d.kind == sources.SOURCE_KIND_CURATED
    # A curated list has no real sort / recommended-only — surfaced honestly.
    assert d.supports_sort is False
    assert d.supports_recommended_only is False
    assert d.variant_noun == "size"


# --- Hugging Face source maps hf_catalog -----------------------------------


@pytest.mark.asyncio
async def test_hf_source_search_maps_catalog_models(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_search(query: str, sort: str, limit: int, **kw: Any) -> list[Any]:
        return [
            hf_catalog.CatalogModel(
                id="bartowski/Qwen2.5-7B-Instruct-GGUF",
                downloads=123,
                likes=4,
                last_modified="2025-01-01",
                recommended=True,
                params_b=7.0,
            )
        ]

    monkeypatch.setattr(hf_catalog, "search", _fake_search)
    src = sources.HuggingFaceSource()
    models = await src.search("qwen", "downloads", 25)
    assert len(models) == 1
    m = models[0]
    assert m.id == "bartowski/Qwen2.5-7B-Instruct-GGUF"
    assert m.downloads == 123
    assert m.recommended is True
    assert m.params_b == 7.0


@pytest.mark.asyncio
async def test_hf_source_variants_build_hf_pull_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_quants(model_id: str, **kw: Any) -> list[Any]:
        return [
            hf_catalog.Quant(
                filename="model-Q4_K_M.gguf",
                quant_label="Q4_K_M",
                size_bytes=4_500_000_000,
                pullable=True,
            ),
            hf_catalog.Quant(
                filename="model-unlabeled.gguf",
                quant_label=None,
                size_bytes=None,
                pullable=False,
            ),
        ]

    monkeypatch.setattr(hf_catalog, "list_quants", _fake_quants)
    src = sources.HuggingFaceSource()
    variants = await src.list_variants("owner/Repo-GGUF")
    assert len(variants) == 2
    v0 = variants[0]
    assert v0.pull_ref == "hf.co/owner/Repo-GGUF:Q4_K_M"
    assert v0.size_estimated is False
    assert v0.size_bytes == 4_500_000_000
    assert v0.pullable is True
    # A labelless gguf can't build a ref ⇒ empty pull_ref, non-pullable.
    v1 = variants[1]
    assert v1.pull_ref == ""
    assert v1.pullable is False


# --- Ollama source serves the curated list ---------------------------------


@pytest.mark.asyncio
async def test_ollama_source_search_is_curated_and_filterable() -> None:
    src = sources.OllamaLibrarySource()
    models = await src.search("coder", "downloads", 25)
    assert models
    assert all(m.recommended for m in models)  # whole list is curated
    assert any(m.id == "qwen2.5-coder" for m in models)
    # Friendly display name carried through.
    assert all(m.name for m in models)


@pytest.mark.asyncio
async def test_ollama_source_variants_are_bare_registry_tags() -> None:
    src = sources.OllamaLibrarySource()
    variants = await src.list_variants("llama3.3")
    assert variants
    v = variants[0]
    assert v.pull_ref == "llama3.3:70b"
    assert not v.pull_ref.startswith("hf.co/")
    # Sizes are estimated for Ollama (no per-tag size API), flagged as such.
    assert v.size_estimated is True
    assert v.size_bytes is not None
    assert v.pullable is True
    assert v.params_b == 70.0


@pytest.mark.asyncio
async def test_ollama_source_unknown_model_has_no_variants() -> None:
    src = sources.OllamaLibrarySource()
    assert await src.list_variants("not-a-real-model") == []
