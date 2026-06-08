"""HF catalog client tests — query building, sort mapping, defensive parsing.

Mirrors ``std-plugins/ollama/tests/test_ollama_ai.py``: a ``MagicMock``
response with a scripted ``.json()`` and an ``AsyncMock`` ``client.get``,
so no real network. The catalog functions accept an injectable ``client``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_model_manager.hf_catalog import (
    SORT_KEY_MAP,
    CatalogModel,
    Quant,
    list_quants,
    parse_quant_label,
    search,
)
from gilbert_plugin_model_manager.recommended import RECOMMENDED_MODELS


def _fake_client(json_payload: Any) -> Any:
    """An object with an ``AsyncMock`` ``get`` returning a scripted JSON."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_payload
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    return client


# --- search() query building + sort mapping -------------------------------


@pytest.mark.asyncio
async def test_search_builds_gguf_filtered_query() -> None:
    client = _fake_client([])
    await search("qwen", "downloads", 25, client=client)

    url = client.get.call_args[0][0]
    params = client.get.call_args[1]["params"]
    assert url.endswith("/api/models")
    assert params["filter"] == "gguf"
    assert params["search"] == "qwen"
    assert params["sort"] == "downloads"
    assert params["direction"] == "-1"
    assert params["limit"] == "25"


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
async def test_search_maps_sort_keys(ui_sort: str, hf_sort: str) -> None:
    client = _fake_client([])
    await search("", ui_sort, 10, client=client)
    assert client.get.call_args[1]["params"]["sort"] == hf_sort
    # The map constant agrees with the actual request.
    assert SORT_KEY_MAP[ui_sort] == hf_sort


@pytest.mark.asyncio
async def test_search_unknown_sort_defaults_to_downloads() -> None:
    client = _fake_client([])
    await search("", "bogus", 10, client=client)
    assert client.get.call_args[1]["params"]["sort"] == "downloads"


# --- search() result shaping + recommended flag ---------------------------


@pytest.mark.asyncio
async def test_search_tags_recommended_and_parses_fields() -> None:
    rec_id = RECOMMENDED_MODELS[0].repo_id
    payload = [
        {
            "id": rec_id,
            "downloads": 12345,
            "likes": 67,
            "lastModified": "2026-01-02T03:04:05.000Z",
        },
        {
            "id": "someone/Random-Model-GGUF",
            "downloads": 5,
            "likes": 0,
            "lastModified": "2025-12-01T00:00:00.000Z",
        },
    ]
    client = _fake_client(payload)
    results = await search("", "downloads", 10, client=client)

    assert [r.id for r in results] == [rec_id, "someone/Random-Model-GGUF"]
    assert all(isinstance(r, CatalogModel) for r in results)
    rec, other = results
    assert rec.recommended is True
    assert rec.downloads == 12345
    assert rec.likes == 67
    assert rec.last_modified == "2026-01-02T03:04:05.000Z"
    assert other.recommended is False


@pytest.mark.asyncio
async def test_search_is_defensive_about_missing_fields() -> None:
    payload = [
        {"id": "owner/A-GGUF"},  # no downloads/likes/lastModified
        {"modelId": "owner/B-GGUF", "downloads": "not-a-number"},  # legacy key
        {"no_id": "skip me"},  # missing id → dropped
        "not-a-dict",  # junk → dropped
    ]
    client = _fake_client(payload)
    results = await search("", "downloads", 10, client=client)

    assert [r.id for r in results] == ["owner/A-GGUF", "owner/B-GGUF"]
    assert results[0].downloads == 0
    assert results[0].likes == 0
    assert results[0].last_modified is None
    assert results[1].downloads == 0  # uncoercible → 0


@pytest.mark.asyncio
async def test_search_non_list_payload_yields_empty() -> None:
    client = _fake_client({"error": "rate limited"})
    assert await search("", "downloads", 10, client=client) == []


# --- list_quants() filtering + parsing ------------------------------------


@pytest.mark.asyncio
async def test_list_quants_filters_to_gguf_and_parses() -> None:
    payload = {
        "siblings": [
            {"rfilename": "README.md"},
            {"rfilename": "config.json"},
            {
                "rfilename": "Llama-3.3-70B-Instruct-Q4_K_M.gguf",
                "size": 42_000_000_000,
            },
            {
                "rfilename": "Llama-3.3-70B-Instruct-Q8_0.gguf",
                "size": 75_000_000_000,
            },
        ]
    }
    client = _fake_client(payload)
    quants = await list_quants("bartowski/Llama-3.3-70B-Instruct-GGUF", client=client)

    assert all(isinstance(q, Quant) for q in quants)
    assert [q.filename for q in quants] == [
        "Llama-3.3-70B-Instruct-Q4_K_M.gguf",
        "Llama-3.3-70B-Instruct-Q8_0.gguf",
    ]
    assert [q.quant_label for q in quants] == ["Q4_K_M", "Q8_0"]
    assert [q.size_bytes for q in quants] == [42_000_000_000, 75_000_000_000]


@pytest.mark.asyncio
async def test_list_quants_hits_blobs_endpoint() -> None:
    client = _fake_client({"siblings": []})
    await list_quants("owner/Repo-GGUF", client=client)
    url = client.get.call_args[0][0]
    params = client.get.call_args[1]["params"]
    assert url.endswith("/api/models/owner/Repo-GGUF")
    assert params["blobs"] == "true"


@pytest.mark.asyncio
async def test_list_quants_reads_lfs_size_fallback() -> None:
    payload = {
        "siblings": [
            {
                "rfilename": "model-Q5_K_M.gguf",
                "lfs": {"size": 5_500_000_000},
            },
        ]
    }
    client = _fake_client(payload)
    quants = await list_quants("owner/Repo-GGUF", client=client)
    assert quants[0].size_bytes == 5_500_000_000
    assert quants[0].quant_label == "Q5_K_M"


@pytest.mark.asyncio
async def test_list_quants_tolerates_no_gguf() -> None:
    payload = {"siblings": [{"rfilename": "README.md"}, {"rfilename": "tokenizer.json"}]}
    client = _fake_client(payload)
    assert await list_quants("owner/Safetensors-Only", client=client) == []


@pytest.mark.asyncio
async def test_list_quants_tolerates_missing_siblings() -> None:
    client = _fake_client({})  # no siblings key
    assert await list_quants("owner/Repo", client=client) == []


@pytest.mark.asyncio
async def test_list_quants_size_unknown_when_absent() -> None:
    payload = {"siblings": [{"rfilename": "model-Q4_K_M.gguf"}]}  # no size
    client = _fake_client(payload)
    quants = await list_quants("owner/Repo-GGUF", client=client)
    assert quants[0].size_bytes is None
    assert quants[0].quant_label == "Q4_K_M"


# --- quant label parsing edge cases ---------------------------------------


@pytest.mark.parametrize(
    "filename,label",
    [
        ("Llama-3.3-70B-Instruct-Q4_K_M.gguf", "Q4_K_M"),
        ("model.Q8_0.gguf", "Q8_0"),
        ("qwen2.5-coder-7b-instruct-q6_k.gguf", "Q6_K"),
        ("model-IQ3_XXS.gguf", "IQ3_XXS"),
        ("model-f16.gguf", "F16"),
        ("plain-model.gguf", None),
    ],
)
def test_parse_quant_label(filename: str, label: str | None) -> None:
    assert parse_quant_label(filename) == label
