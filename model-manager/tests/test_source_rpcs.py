"""WS RPC tests for the multi-source installer (S10).

Exercises ``model_manager.sources.list`` / ``.source.search`` /
``.source.variants`` at the service-method seam: admin gating, the unknown-source
error, the serialized frame shapes, and that variants carry a hardware-fit
verdict. The sources' catalog clients are monkeypatched so no real network I/O.
"""

from __future__ import annotations

from typing import Any

import pytest
from gilbert_plugin_model_manager import hf_catalog, sources
from gilbert_plugin_model_manager.model_manager_service import ModelManagerService


class _FakeRuntime:
    async def list_models(self) -> list[Any]:  # pragma: no cover - unused
        return []

    async def pull_model(self, ref: str, on_progress: Any = None) -> None:  # pragma: no cover
        return None

    async def delete_model(self, tag: str) -> None:  # pragma: no cover - unused
        return None

    def base_url(self) -> str:
        return "http://localhost:11434"


class _FakeResolver:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def get_capability(self, name: str) -> Any:
        return self._runtime if name == "local_model_runtime" else None

    def require_capability(self, name: str) -> Any:
        if name == "local_model_runtime":
            return self._runtime
        raise KeyError(name)

    def get_all(self, name: str) -> list[Any]:  # pragma: no cover - unused
        return []


class _Conn:
    def __init__(self, user_level: int = 0) -> None:
        self.user_level = user_level


@pytest.fixture
async def svc() -> Any:
    service = ModelManagerService()
    await service.start(_FakeResolver(_FakeRuntime()))
    yield service
    await service.stop()


# --- handler registration --------------------------------------------------


def test_source_handlers_registered() -> None:
    handlers = ModelManagerService().get_ws_handlers()
    assert "model_manager.sources.list" in handlers
    assert "model_manager.source.search" in handlers
    assert "model_manager.source.variants" in handlers


# --- sources.list ----------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_list_returns_descriptors(svc: Any) -> None:
    resp = await svc._ws_sources_list(_Conn(0), {"id": "s1"})
    assert resp["type"] == "model_manager.sources.list.result"
    ids = [s["id"] for s in resp["sources"]]
    assert ids == [sources.HUGGINGFACE_SOURCE_ID, sources.OLLAMA_SOURCE_ID]
    hf = next(s for s in resp["sources"] if s["id"] == sources.HUGGINGFACE_SOURCE_ID)
    assert hf["kind"] == "search"
    assert hf["supports_sort"] is True
    ollama = next(s for s in resp["sources"] if s["id"] == sources.OLLAMA_SOURCE_ID)
    assert ollama["kind"] == "curated"
    assert ollama["supports_sort"] is False


@pytest.mark.asyncio
async def test_sources_list_is_admin_gated(svc: Any) -> None:
    resp = await svc._ws_sources_list(_Conn(1), {"id": "s2"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403


# --- source.search ---------------------------------------------------------


@pytest.mark.asyncio
async def test_source_search_unknown_source_400(svc: Any) -> None:
    resp = await svc._ws_source_search(_Conn(0), {"id": "s3", "source": "bogus"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 400
    assert "bogus" in resp["error"]


@pytest.mark.asyncio
async def test_source_search_ollama_returns_curated_list(svc: Any) -> None:
    resp = await svc._ws_source_search(
        _Conn(0), {"id": "s4", "source": sources.OLLAMA_SOURCE_ID, "query": "coder"}
    )
    assert resp["type"] == "model_manager.source.search.result"
    assert resp["source"] == sources.OLLAMA_SOURCE_ID
    ids = [m["id"] for m in resp["models"]]
    assert "qwen2.5-coder" in ids
    # Curated rows carry a friendly name and the recommended flag.
    row = next(m for m in resp["models"] if m["id"] == "qwen2.5-coder")
    assert row["name"]
    assert row["recommended"] is True


@pytest.mark.asyncio
async def test_source_search_hf_passthrough(svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_search(query: str, sort: str, limit: int, **kw: Any) -> list[Any]:
        return [
            hf_catalog.CatalogModel(
                id="owner/Repo-GGUF",
                downloads=10,
                likes=1,
                last_modified=None,
                recommended=False,
                params_b=7.0,
            )
        ]

    monkeypatch.setattr(hf_catalog, "search", _fake_search)
    resp = await svc._ws_source_search(
        _Conn(0), {"id": "s5", "source": sources.HUGGINGFACE_SOURCE_ID, "query": "repo"}
    )
    assert resp["type"] == "model_manager.source.search.result"
    assert resp["models"][0]["id"] == "owner/Repo-GGUF"
    assert resp["models"][0]["params_b"] == 7.0


@pytest.mark.asyncio
async def test_source_search_is_admin_gated(svc: Any) -> None:
    resp = await svc._ws_source_search(
        _Conn(1), {"id": "s6", "source": sources.OLLAMA_SOURCE_ID}
    )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403


# --- source.variants -------------------------------------------------------


@pytest.mark.asyncio
async def test_source_variants_ollama_tags_with_fit(svc: Any) -> None:
    resp = await svc._ws_source_variants(
        _Conn(0),
        {"id": "s7", "source": sources.OLLAMA_SOURCE_ID, "model_id": "llama3.3"},
    )
    assert resp["type"] == "model_manager.source.variants.result"
    assert resp["source"] == sources.OLLAMA_SOURCE_ID
    assert resp["model_id"] == "llama3.3"
    variants = resp["variants"]
    assert variants
    v = variants[0]
    assert v["pull_ref"] == "llama3.3:70b"
    assert v["size_estimated"] is True
    # A fit verdict is always attached (unknown here: no host_resources cap).
    assert v["fit"] in ("fits-vram", "fits-ram", "wont-fit", "unknown")


@pytest.mark.asyncio
async def test_source_variants_hf_quants_with_fit(svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_quants(model_id: str, **kw: Any) -> list[Any]:
        return [
            hf_catalog.Quant(
                filename="m-Q4_K_M.gguf",
                quant_label="Q4_K_M",
                size_bytes=4_000_000_000,
                pullable=True,
            )
        ]

    monkeypatch.setattr(hf_catalog, "list_quants", _fake_quants)
    resp = await svc._ws_source_variants(
        _Conn(0),
        {
            "id": "s8",
            "source": sources.HUGGINGFACE_SOURCE_ID,
            "model_id": "owner/Repo-GGUF",
        },
    )
    assert resp["type"] == "model_manager.source.variants.result"
    v = resp["variants"][0]
    assert v["pull_ref"] == "hf.co/owner/Repo-GGUF:Q4_K_M"
    assert v["size_estimated"] is False
    assert v["size_bytes"] == 4_000_000_000


@pytest.mark.asyncio
async def test_source_variants_requires_model_id(svc: Any) -> None:
    resp = await svc._ws_source_variants(
        _Conn(0), {"id": "s9", "source": sources.OLLAMA_SOURCE_ID}
    )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 400


@pytest.mark.asyncio
async def test_source_variants_unknown_source_400(svc: Any) -> None:
    resp = await svc._ws_source_variants(
        _Conn(0), {"id": "s10", "source": "bogus", "model_id": "x"}
    )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 400


@pytest.mark.asyncio
async def test_source_variants_is_admin_gated(svc: Any) -> None:
    resp = await svc._ws_source_variants(
        _Conn(1), {"id": "s11", "source": sources.OLLAMA_SOURCE_ID, "model_id": "llama3.3"}
    )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403
