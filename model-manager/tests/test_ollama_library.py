"""Tests for the curated Ollama-registry catalog (S10).

Pure-function tests over :mod:`ollama_library` — no network, no service. Assert
the curated list filters honestly, that size tags build the correct bare
registry pull refs, and that the size *estimate* is present (flagged upstream as
estimated, never presented as exact).
"""

from __future__ import annotations

from gilbert_plugin_model_manager import ollama_library


def test_search_empty_query_returns_all() -> None:
    models = ollama_library.search("", 100)
    assert len(models) == len(ollama_library.OLLAMA_MODELS)


def test_search_filters_by_substring_case_insensitive() -> None:
    models = ollama_library.search("CODER", 100)
    assert models, "expected at least one coder model"
    assert all("coder" in m.name.lower() or "coder" in m.display_name.lower() for m in models)
    assert any(m.name == "qwen2.5-coder" for m in models)


def test_search_matches_description() -> None:
    # "reasoning" appears in descriptions (phi4 / deepseek-r1), not names.
    models = ollama_library.search("reasoning", 100)
    assert models
    assert any(m.name == "deepseek-r1" for m in models)


def test_search_respects_limit() -> None:
    models = ollama_library.search("", 3)
    assert len(models) == 3


def test_list_tags_builds_bare_registry_pull_refs() -> None:
    tags = ollama_library.list_tags("llama3.3")
    assert tags
    # 70b → pull ref ``llama3.3:70b`` (a bare registry tag, NOT an hf.co ref).
    tag = tags[0]
    assert tag.label == "70b"
    assert tag.pull_ref == "llama3.3:70b"
    assert not tag.pull_ref.startswith("hf.co/")
    assert tag.params_b == 70.0


def test_list_tags_estimates_size_from_params() -> None:
    tags = ollama_library.list_tags("qwen2.5")
    by_label = {t.label: t for t in tags}
    seven = by_label["7b"]
    assert seven.estimated_size_bytes is not None
    # 7B × 0.6 GB/B ≈ 4.2 GB — a sane Q4_K_M ballpark.
    assert 3_000_000_000 < seven.estimated_size_bytes < 6_000_000_000


def test_list_tags_decimal_size_label() -> None:
    tags = ollama_library.list_tags("qwen2.5")
    labels = {t.label for t in tags}
    assert "0.5b" in labels
    assert "1.5b" in labels


def test_list_tags_unknown_model_is_empty() -> None:
    assert ollama_library.list_tags("does-not-exist") == []


def test_estimate_size_none_for_unknown_params() -> None:
    assert ollama_library.estimate_size_bytes(None) is None
    assert ollama_library.estimate_size_bytes(0) is None


def test_largest_params_b() -> None:
    model = ollama_library.get_model("qwen2.5")
    assert model is not None
    assert ollama_library.largest_params_b(model) == 72.0
