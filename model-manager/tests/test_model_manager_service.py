"""ModelManagerService tests — capability advertisement, the enablement
dependency on the Ollama backend, the full ``Configurable`` protocol
surface, and the installed-models RPC against a faked runtime."""

from __future__ import annotations

from typing import Any

import pytest
from gilbert_plugin_model_manager.model_manager_service import (
    ModelManagerService,
)

from gilbert.interfaces.configuration import Configurable
from gilbert.interfaces.local_models import (
    InstalledModel,
    LocalModelRuntimeProvider,
)
from gilbert.interfaces.service import EnablementDep
from gilbert.interfaces.ws import WsHandlerProvider


class _FakeRuntime:
    """Duck-typed ``LocalModelRuntimeProvider`` — records calls and returns
    a scripted installed-models list. ``isinstance`` against the
    ``@runtime_checkable`` protocol passes because the four methods exist."""

    def __init__(self, models: list[InstalledModel] | None = None) -> None:
        self._models = models or []
        self.list_calls = 0

    async def list_models(self) -> list[InstalledModel]:
        self.list_calls += 1
        return list(self._models)

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
        if name == "local_model_runtime":
            return self._runtime
        return None

    def require_capability(self, name: str) -> Any:
        if name == "local_model_runtime":
            return self._runtime
        raise KeyError(name)

    def get_all(self, name: str) -> list[Any]:  # pragma: no cover - unused
        return [self._runtime] if name == "local_model_runtime" else []


class _Conn:
    """Minimal WS connection stub. ``user_level == 0`` ⇒ admin."""

    def __init__(self, user_level: int = 0) -> None:
        self.user_level = user_level


@pytest.fixture
async def started_service() -> Any:
    runtime = _FakeRuntime(
        models=[
            InstalledModel(tag="llama3.3", size_bytes=42_000_000_000),
            InstalledModel(tag="qwen2.5-coder:32b", size_bytes=None),
        ]
    )
    svc = ModelManagerService()
    await svc.start(_FakeResolver(runtime))
    yield svc, runtime
    await svc.stop()


def test_service_advertises_model_manager_capability() -> None:
    info = ModelManagerService().service_info()
    assert "model_manager" in info.capabilities
    # The page's UIRoute gates on this capability; the WS RPC needs the
    # ws_handlers capability to be dispatched.
    assert "ws_handlers" in info.capabilities
    assert info.name == "model_manager"
    assert info.toggleable is True
    assert info.toggle_description


def test_service_declares_local_runtime_requirement() -> None:
    info = ModelManagerService().service_info()
    assert "local_model_runtime" in info.requires


def test_service_declares_ollama_enablement_dependency() -> None:
    """The manager must not start unless the Ollama backend is *enabled* —
    declared as an enablement dependency on the ``ai_chat`` service's
    ``ollama`` backend (ADR-0018)."""
    info = ModelManagerService().service_info()
    assert EnablementDep(capability="ai_chat", backend="ollama") in (info.requires_enabled)


def test_service_satisfies_configurable_protocol() -> None:
    """A toggleable service must implement the full ``Configurable``
    protocol or its toggle won't render."""
    svc = ModelManagerService()
    assert isinstance(svc, Configurable)
    assert svc.config_namespace == "model-manager"
    assert svc.config_category
    params = svc.config_params()
    assert isinstance(params, list)
    assert all(p.key for p in params)


def test_service_implements_ws_handler_provider() -> None:
    svc = ModelManagerService()
    assert isinstance(svc, WsHandlerProvider)
    assert "model_manager.installed.list" in svc.get_ws_handlers()


@pytest.mark.asyncio
async def test_start_resolves_runtime_and_enables(started_service: Any) -> None:
    svc, runtime = started_service
    assert svc.enabled is True
    # The runtime resolved is the provider-neutral capability, not a
    # concrete class.
    assert isinstance(svc._runtime, LocalModelRuntimeProvider)


@pytest.mark.asyncio
async def test_installed_list_returns_models_from_runtime(
    started_service: Any,
) -> None:
    svc, runtime = started_service
    resp = await svc._ws_installed_list(_Conn(user_level=0), {"id": "req-1"})
    assert resp["type"] == "model_manager.installed.list.result"
    assert resp["ref"] == "req-1"
    assert runtime.list_calls == 1
    assert resp["models"] == [
        {"tag": "llama3.3", "size_bytes": 42_000_000_000},
        {"tag": "qwen2.5-coder:32b", "size_bytes": None},
    ]


@pytest.mark.asyncio
async def test_installed_list_is_admin_gated(started_service: Any) -> None:
    svc, runtime = started_service
    # user_level > 0 ⇒ non-admin ⇒ denied before touching the runtime.
    resp = await svc._ws_installed_list(_Conn(user_level=1), {"id": "req-2"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403
    assert runtime.list_calls == 0


@pytest.mark.asyncio
async def test_installed_list_surfaces_runtime_failure() -> None:
    class _ExplodingRuntime(_FakeRuntime):
        async def list_models(self) -> list[InstalledModel]:
            raise RuntimeError("daemon unreachable")

    svc = ModelManagerService()
    await svc.start(_FakeResolver(_ExplodingRuntime()))
    resp = await svc._ws_installed_list(_Conn(user_level=0), {"id": "req-3"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 502
    assert "daemon unreachable" in resp["error"]
    await svc.stop()


def test_create_plugin_declares_models_route() -> None:
    from gilbert_plugin_model_manager.plugin import create_plugin

    plugin = create_plugin()
    routes = plugin.ui_routes()
    assert len(routes) == 1
    route = routes[0]
    assert route.path == "/models"
    assert route.panel_id == "model-manager.page"
    assert route.required_role == "admin"
    # The route hides from nav + the SPA route table when the manager
    # isn't live (unmet enablement dep or toggled off).
    assert route.requires_capability == "model_manager"
    assert route.add_to_nav is True
