from __future__ import annotations

from gilbert_plugin_mafia.plugin import create_plugin
from gilbert_plugin_mafia.service import MafiaService

from gilbert.interfaces.configuration import Configurable
from gilbert.interfaces.service import EnablementDep


def test_plugin_metadata() -> None:
    plugin = create_plugin()
    meta = plugin.metadata()
    assert meta.name == "mafia"
    assert "mafia_game" in meta.provides


def test_ui_route_is_guest_visible() -> None:
    routes = create_plugin().ui_routes()
    assert len(routes) == 1
    r = routes[0]
    assert r.path == "/mafia"
    assert r.panel_id == "mafia.page"
    assert r.required_role == "everyone"
    assert r.requires_capability == "mafia_game"


def test_service_info() -> None:
    svc = MafiaService()
    info = svc.service_info()
    assert info.name == "mafia"
    assert {"mafia_game", "ws_handlers", "ai_tools"} <= set(info.capabilities)
    assert EnablementDep(capability="text_to_speech") in info.requires_enabled
    assert info.toggleable is True
    assert isinstance(svc, Configurable)
    assert svc.config_namespace == "mafia"
    assert MafiaService.slash_namespace == "mafia"
