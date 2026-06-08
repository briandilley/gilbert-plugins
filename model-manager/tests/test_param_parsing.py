"""Pure-function tests for parameter-count parsing + the powerful/smallest
re-rank (S9, browse/pull fixes).

``parse_param_count`` is the test seam for the "Most powerful" sort: it turns
a repo id / filename into an approximate parameter count in **billions**.
The ``search()`` re-rank for ``powerful`` / ``smallest`` is exercised with a
mocked HF page (no network) to assert it re-orders by ``params_b`` and sinks
unknowns, while the existing HF-native sorts stay pass-through.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_model_manager.hf_catalog import (
    CatalogModel,
    parse_param_count,
    search,
)


def _fake_client(json_payload: Any) -> Any:
    """An object with an ``AsyncMock`` ``get`` returning a scripted JSON."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_payload
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    return client


# --- parse_param_count() --------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("bartowski/Llama-3.3-7B-Instruct-GGUF", 7.0),
        ("bartowski/Llama-3.3-70B-Instruct-GGUF", 70.0),
        # MoE total = experts × per-expert size.
        ("mistralai/Mixtral-8x7B-Instruct-GGUF", 56.0),
        ("someone/Mixtral-8x22B-Instruct-GGUF", 176.0),
        # Active/total split: take the larger (total) token, ignore A3B active.
        ("Qwen/Qwen3-35B-A3B-GGUF", 35.0),
        # Decimal billions.
        ("microsoft/Phi-3.8B-mini-GGUF", 3.8),
        # Millions → fractional billions.
        ("owner/SmolLM-500M-Instruct-GGUF", 0.5),
        # No recognizable token → None.
        ("bartowski/Phi-4-GGUF", None),
        ("owner/just-a-name", None),
    ],
)
def test_parse_param_count(name: str, expected: float | None) -> None:
    got = parse_param_count(name)
    if expected is None:
        assert got is None
    else:
        assert got is not None
        assert got == pytest.approx(expected)


def test_parse_param_count_prefers_param_token_over_version_digits() -> None:
    # The ``3.3`` version must not be mistaken for params; the ``8B`` is.
    assert parse_param_count("meta/Llama-3.3-8B-Instruct") == pytest.approx(8.0)


# --- search() re-rank for powerful / smallest -----------------------------

_MIXED_PAGE = [
    {"id": "owner/Tiny-1B-GGUF", "downloads": 1000, "likes": 1},
    {"id": "owner/Huge-70B-GGUF", "downloads": 10, "likes": 0},
    {"id": "owner/Mid-13B-GGUF", "downloads": 500, "likes": 5},
    # No recognizable param token → params_b is None → sinks to the bottom.
    {"id": "owner/Mystery-GGUF", "downloads": 9999, "likes": 99},
]


@pytest.mark.asyncio
async def test_powerful_sort_reranks_by_params_desc_with_unknown_last() -> None:
    client = _fake_client(_MIXED_PAGE)
    results = await search("", "powerful", 25, client=client)

    assert [m.id for m in results] == [
        "owner/Huge-70B-GGUF",
        "owner/Mid-13B-GGUF",
        "owner/Tiny-1B-GGUF",
        "owner/Mystery-GGUF",  # unknown params → last
    ]
    assert all(isinstance(m, CatalogModel) for m in results)
    by_id = {m.id: m for m in results}
    assert by_id["owner/Huge-70B-GGUF"].params_b == pytest.approx(70.0)
    assert by_id["owner/Mystery-GGUF"].params_b is None


@pytest.mark.asyncio
async def test_smallest_sort_reranks_by_params_asc_with_unknown_last() -> None:
    client = _fake_client(_MIXED_PAGE)
    results = await search("", "smallest", 25, client=client)

    assert [m.id for m in results] == [
        "owner/Tiny-1B-GGUF",
        "owner/Mid-13B-GGUF",
        "owner/Huge-70B-GGUF",
        "owner/Mystery-GGUF",  # unknown params still sink to the bottom
    ]


@pytest.mark.asyncio
async def test_powerful_sort_fetches_larger_page_ordered_by_downloads() -> None:
    """The derived sort can't be a true HF sort, so it fetches a LARGER base
    page (ordered by a real HF field) and re-ranks the top matches."""
    client = _fake_client(_MIXED_PAGE)
    await search("qwen", "powerful", 10, client=client)

    params = client.get.call_args[1]["params"]
    # Base HF field is a real one (downloads), never the synthetic key.
    assert params["sort"] == "downloads"
    # Larger page so the re-rank sees more than ``limit`` candidates.
    assert int(params["limit"]) >= 100


@pytest.mark.asyncio
async def test_powerful_sort_truncates_to_limit() -> None:
    client = _fake_client(_MIXED_PAGE)
    results = await search("", "powerful", 2, client=client)
    assert len(results) == 2
    # Top-2 by params.
    assert [m.id for m in results] == ["owner/Huge-70B-GGUF", "owner/Mid-13B-GGUF"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ui_sort,hf_sort",
    [
        ("downloads", "downloads"),
        ("likes", "likes"),
        ("trending", "trendingScore"),
        ("recent", "lastModified"),
    ],
)
async def test_existing_sorts_are_unaffected_passthrough(ui_sort: str, hf_sort: str) -> None:
    client = _fake_client(_MIXED_PAGE)
    results = await search("", ui_sort, 25, client=client)
    # The HF sort field is the native one and the page order is preserved
    # (no client-side re-rank for the pass-through sorts).
    assert client.get.call_args[1]["params"]["sort"] == hf_sort
    assert [m.id for m in results] == [e["id"] for e in _MIXED_PAGE]


@pytest.mark.asyncio
async def test_param_count_attached_to_every_model() -> None:
    """Even on a pass-through sort, ``params_b`` is parsed so the UI chip and
    the client-side size-class filter have data to work with."""
    client = _fake_client(_MIXED_PAGE)
    results = await search("", "downloads", 25, client=client)
    by_id = {m.id: m.params_b for m in results}
    assert by_id["owner/Tiny-1B-GGUF"] == pytest.approx(1.0)
    assert by_id["owner/Mid-13B-GGUF"] == pytest.approx(13.0)
    assert by_id["owner/Mystery-GGUF"] is None
