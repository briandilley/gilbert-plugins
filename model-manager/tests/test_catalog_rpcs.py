"""Catalog WS RPC tests — ``model_manager.catalog.search`` / ``.quants``.

Asserts the serialized frame shapes, the admin gate (mirroring
``installed.list``), the recommended flag passthrough, and graceful errors.
The plugin's catalog client is monkeypatched so no real HF network I/O —
the *service-method seam* is what's under test, not httpx itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from gilbert_plugin_model_manager import hf_catalog
from gilbert_plugin_model_manager.hf_catalog import CatalogModel, Quant
from gilbert_plugin_model_manager.model_manager_service import ModelManagerService


class _FakeRuntime:
    """Minimal duck-typed ``LocalModelRuntimeProvider`` for ``start()``."""

    async def list_models(self) -> list[Any]:  # pragma: no cover - unused here
        return []

    async def pull_model(self, ref: str) -> None:  # pragma: no cover - unused
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
        return [self._runtime] if name == "local_model_runtime" else []


class _Conn:
    def __init__(self, user_level: int = 0) -> None:
        self.user_level = user_level


@pytest.fixture
async def svc() -> Any:
    service = ModelManagerService()
    await service.start(_FakeResolver(_FakeRuntime()))
    yield service
    await service.stop()


# --- catalog.search -------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_search_returns_serialized_results(
    svc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_search(query: str, sort: str, limit: int, **kw: Any) -> list[CatalogModel]:
        captured.update(query=query, sort=sort, limit=limit)
        return [
            CatalogModel(
                id="bartowski/Llama-3.3-70B-Instruct-GGUF",
                downloads=12345,
                likes=67,
                last_modified="2026-01-02T03:04:05.000Z",
                recommended=True,
            ),
            CatalogModel(
                id="someone/Random-GGUF",
                downloads=5,
                likes=0,
                last_modified=None,
                recommended=False,
            ),
        ]

    monkeypatch.setattr(hf_catalog, "search", fake_search)

    resp = await svc._ws_catalog_search(
        _Conn(0),
        {"id": "req-1", "query": "llama", "sort": "downloads", "limit": 25},
    )

    assert resp["type"] == "model_manager.catalog.search.result"
    assert resp["ref"] == "req-1"
    assert captured == {"query": "llama", "sort": "downloads", "limit": 25}
    assert resp["models"] == [
        {
            "id": "bartowski/Llama-3.3-70B-Instruct-GGUF",
            "downloads": 12345,
            "likes": 67,
            "last_modified": "2026-01-02T03:04:05.000Z",
            "recommended": True,
        },
        {
            "id": "someone/Random-GGUF",
            "downloads": 5,
            "likes": 0,
            "last_modified": None,
            "recommended": False,
        },
    ]


@pytest.mark.asyncio
async def test_catalog_search_defaults_args(svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_search(query: str, sort: str, limit: int, **kw: Any) -> list[CatalogModel]:
        captured.update(query=query, sort=sort, limit=limit)
        return []

    monkeypatch.setattr(hf_catalog, "search", fake_search)
    await svc._ws_catalog_search(_Conn(0), {"id": "req-2"})
    # Missing args fall back to sane defaults, not a crash.
    assert captured["query"] == ""
    assert captured["sort"] == "downloads"
    assert isinstance(captured["limit"], int) and captured["limit"] > 0


@pytest.mark.asyncio
async def test_catalog_search_is_admin_gated(svc: Any) -> None:
    resp = await svc._ws_catalog_search(_Conn(1), {"id": "req-3", "query": "x"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403


@pytest.mark.asyncio
async def test_catalog_search_surfaces_failure(svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: Any, **k: Any) -> list[CatalogModel]:
        raise RuntimeError("hub unreachable")

    monkeypatch.setattr(hf_catalog, "search", boom)
    resp = await svc._ws_catalog_search(_Conn(0), {"id": "req-4", "query": "x"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 502
    assert "hub unreachable" in resp["error"]


# --- catalog.quants -------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_quants_returns_serialized_quants(
    svc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_quants(model_id: str, **kw: Any) -> list[Quant]:
        captured["model_id"] = model_id
        return [
            Quant(filename="m-Q4_K_M.gguf", quant_label="Q4_K_M", size_bytes=42_000_000_000),
            Quant(filename="m-Q8_0.gguf", quant_label="Q8_0", size_bytes=None),
        ]

    monkeypatch.setattr(hf_catalog, "list_quants", fake_quants)

    resp = await svc._ws_catalog_quants(_Conn(0), {"id": "req-5", "model_id": "owner/Repo-GGUF"})

    assert resp["type"] == "model_manager.catalog.quants.result"
    assert resp["ref"] == "req-5"
    assert captured["model_id"] == "owner/Repo-GGUF"
    assert resp["model_id"] == "owner/Repo-GGUF"
    assert resp["quants"] == [
        {"filename": "m-Q4_K_M.gguf", "quant_label": "Q4_K_M", "size_bytes": 42_000_000_000},
        {"filename": "m-Q8_0.gguf", "quant_label": "Q8_0", "size_bytes": None},
    ]


@pytest.mark.asyncio
async def test_catalog_quants_requires_model_id(svc: Any) -> None:
    resp = await svc._ws_catalog_quants(_Conn(0), {"id": "req-6"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 400


@pytest.mark.asyncio
async def test_catalog_quants_is_admin_gated(svc: Any) -> None:
    resp = await svc._ws_catalog_quants(_Conn(1), {"id": "req-7", "model_id": "owner/Repo-GGUF"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403


@pytest.mark.asyncio
async def test_catalog_quants_surfaces_failure(svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: Any, **k: Any) -> list[Quant]:
        raise RuntimeError("repo gone")

    monkeypatch.setattr(hf_catalog, "list_quants", boom)
    resp = await svc._ws_catalog_quants(_Conn(0), {"id": "req-8", "model_id": "owner/Repo-GGUF"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 502
    assert "repo gone" in resp["error"]


# --- handler registration -------------------------------------------------


def test_catalog_handlers_registered() -> None:
    handlers = ModelManagerService().get_ws_handlers()
    assert "model_manager.catalog.search" in handlers
    assert "model_manager.catalog.quants" in handlers
