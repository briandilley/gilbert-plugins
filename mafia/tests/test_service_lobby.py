from __future__ import annotations

from typing import Any

import pytest
from gilbert_plugin_mafia.service import MafiaService

from gilbert.interfaces.auth import UserContext


class _Conn:
    def __init__(self, user_id: str = "usr_1", name: str = "Cam", level: int = 100) -> None:
        self.user_ctx = UserContext(
            user_id=user_id, email="", display_name=name, roles=frozenset({"user"}), provider="local"
        )
        self.user_level = level
        self.sent: list[dict[str, Any]] = []
        self._close_cbs: list[Any] = []

    def enqueue(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    def add_close_callback(self, cb: Any) -> None:
        self._close_cbs.append(cb)

    def close(self) -> None:
        for cb in self._close_cbs:
            cb()


def _guest_conn(name: str = "Guest") -> _Conn:
    return _Conn(user_id="guest", name=name, level=200)


class _FakeResolver:
    def get_capability(self, name: str) -> Any:
        return None

    def require_capability(self, name: str) -> Any:
        raise LookupError(name)

    def get_all(self, name: str) -> list[Any]:
        return []


@pytest.fixture
async def svc() -> MafiaService:
    service = MafiaService()
    service._config = {"enabled": True}
    await service.on_config_changed(service._config)
    service._resolver = _FakeResolver()
    service._enabled = True
    return service


async def _create(svc: MafiaService, conn: _Conn) -> dict[str, Any]:
    resp = await svc._ws_game_create(conn, {"id": "r1", "theme_key": "camping"})
    assert resp["type"] == "mafia.game.create.result", resp
    return resp


async def test_create_requires_real_account(svc: MafiaService) -> None:
    resp = await svc._ws_game_create(_guest_conn(), {"id": "r1", "theme_key": "camping"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403


async def test_create_and_join_flow(svc: MafiaService) -> None:
    host = _Conn()
    created = await _create(svc, host)
    assert created["join_code"]
    assert created["state"]["you"]["is_host"] is True

    guest = _guest_conn("Jess")
    joined = await svc._ws_game_join(
        guest, {"id": "r2", "join_code": created["join_code"], "name": "Jess"}
    )
    assert joined["type"] == "mafia.game.join.result"
    assert joined["player_token"]
    # host got a live state push when Jess joined
    assert any(m["type"] == "mafia.state" for m in host.sent)


async def test_join_bad_code(svc: MafiaService) -> None:
    resp = await svc._ws_game_join(_guest_conn(), {"id": "r", "join_code": "NOPE99", "name": "X"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 404


async def test_resume_reattaches(svc: MafiaService) -> None:
    host = _Conn()
    created = await _create(svc, host)
    guest = _guest_conn("Jess")
    joined = await svc._ws_game_join(
        guest, {"id": "r2", "join_code": created["join_code"], "name": "Jess"}
    )
    guest.close()  # simulates page reload — registry cleaned
    fresh = _guest_conn("Jess")
    resumed = await svc._ws_game_resume(
        fresh,
        {"id": "r3", "game_id": joined["game_id"], "player_token": joined["player_token"]},
    )
    assert resumed["type"] == "mafia.game.resume.result"
    assert resumed["state"]["you"]["name"] == "Jess"


async def test_resume_bad_token(svc: MafiaService) -> None:
    host = _Conn()
    created = await _create(svc, host)
    resp = await svc._ws_game_resume(
        _guest_conn(), {"id": "r", "game_id": created["game_id"], "player_token": "bad"}
    )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403


async def test_max_concurrent_games(svc: MafiaService) -> None:
    svc._max_games = 1
    await _create(svc, _Conn())
    resp = await svc._ws_game_create(_Conn(user_id="usr_2"), {"id": "r", "theme_key": "space"})
    assert resp["type"] == "gilbert.error"


async def test_declared_rpc_roles(svc: MafiaService) -> None:
    assert svc.get_ws_rpc_roles() == {"mafia.": "everyone"}
    handlers = svc.get_ws_handlers()
    assert "mafia.game.join" in handlers and "mafia.game.create" in handlers
