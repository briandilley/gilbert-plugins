from __future__ import annotations

import pytest
from gilbert_plugin_mafia.game import MafiaGame
from gilbert_plugin_mafia.service import MafiaService

from gilbert.interfaces.tools import ToolProvider


@pytest.fixture
async def svc() -> MafiaService:
    service = MafiaService()
    service._config = {"enabled": True}
    await service.on_config_changed(service._config)
    service._enabled = True
    return service


def test_is_tool_provider(svc: MafiaService) -> None:
    assert isinstance(svc, ToolProvider)
    tools = svc.get_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "mafia_open"
    assert tool.required_role == "everyone"
    assert tool.slash_command == "open"
    assert tool.slash_help


async def test_execute_links_page_and_lobbies(svc: MafiaService) -> None:
    text = await svc.execute_tool("mafia_open", {})
    assert "[Open Mafia](/mafia)" in text
    game = MafiaGame(host_user_id="u1", host_name="Cam")
    svc._games[game.game_id] = game
    text = await svc.execute_tool("mafia_open", {})
    assert game.join_code in text


async def test_unknown_tool_raises(svc: MafiaService) -> None:
    with pytest.raises(KeyError):
        await svc.execute_tool("nope", {})
