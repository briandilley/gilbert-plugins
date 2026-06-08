"""``recommended_only`` search path tests (S9, fix the empty Recommended filter).

The bug: "Recommended only" was a client-side post-filter over the generic
top-N HF page, so the ~10 curated repos almost never appeared. The fix
sources the overlay DIRECTLY — ``search_recommended()`` returns the
``RECOMMENDED_MODELS`` set as ``CatalogModel``s, independent of any generic
HF query, so the curated set ALWAYS shows.

These tests assert the path does NOT depend on the downloads-page fetch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_model_manager.hf_catalog import (
    CatalogModel,
    search,
    search_recommended,
)
from gilbert_plugin_model_manager.recommended import RECOMMENDED_MODELS


def _exploding_client() -> Any:
    """A client whose ``get`` raises — proves the path never touches HTTP."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=AssertionError("recommended path must not fetch"))
    return client


@pytest.mark.asyncio
async def test_search_recommended_returns_the_whole_overlay() -> None:
    client = _exploding_client()
    results = await search_recommended(client=client)

    assert all(isinstance(m, CatalogModel) for m in results)
    # Exactly the overlay set, regardless of any generic query.
    assert {m.id for m in results} == {r.repo_id for r in RECOMMENDED_MODELS}
    assert all(m.recommended is True for m in results)
    # Did NOT depend on a downloads-page fetch.
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_search_recommended_parses_params_from_id() -> None:
    results = await search_recommended(client=_exploding_client())
    by_id = {m.id: m for m in results}
    # 70B model parses to ~70 billion params.
    assert by_id["bartowski/Llama-3.3-70B-Instruct-GGUF"].params_b == pytest.approx(70.0)
    # 8B model parses to ~8.
    assert by_id["bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"].params_b == pytest.approx(8.0)


@pytest.mark.asyncio
async def test_search_routes_recommended_only_to_overlay() -> None:
    """``search(..., recommended_only=True)`` returns the overlay set and does
    NOT issue the generic /api/models request."""
    client = _exploding_client()
    results = await search("ignored-query", "downloads", 25, recommended_only=True, client=client)
    assert {m.id for m in results} == {r.repo_id for r in RECOMMENDED_MODELS}
    client.get.assert_not_called()
