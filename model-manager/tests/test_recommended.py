"""Recommended-overlay tests — membership, lookup, and the badge set."""

from __future__ import annotations

from gilbert_plugin_model_manager.recommended import (
    RECOMMENDED_MODELS,
    RecommendedModel,
    is_recommended,
    lookup_recommended,
    recommended_repo_ids,
)


def test_overlay_is_small_and_well_formed() -> None:
    """A curated overlay (~8–12), each with a repo id, name, description."""
    assert 8 <= len(RECOMMENDED_MODELS) <= 12
    for m in RECOMMENDED_MODELS:
        assert isinstance(m, RecommendedModel)
        assert "/" in m.repo_id  # HF repo ids are <owner>/<name>
        assert m.name
        assert m.description


def test_repo_ids_are_unique() -> None:
    ids = [m.repo_id for m in RECOMMENDED_MODELS]
    assert len(ids) == len(set(ids))


def test_recommended_repo_ids_matches_models() -> None:
    ids = recommended_repo_ids()
    assert ids == frozenset(m.repo_id for m in RECOMMENDED_MODELS)


def test_is_recommended_membership() -> None:
    known = RECOMMENDED_MODELS[0].repo_id
    assert is_recommended(known) is True
    assert is_recommended("someone/Not-A-Real-Repo-GGUF") is False


def test_lookup_returns_friendly_entry() -> None:
    known = RECOMMENDED_MODELS[0]
    entry = lookup_recommended(known.repo_id)
    assert entry is not None
    assert entry.name == known.name
    assert entry.description == known.description
    assert lookup_recommended("nope/nope") is None
