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


async def test_create_with_blank_display_name_falls_back(svc: MafiaService) -> None:
    """M3: an empty host display_name falls back to user_id, then 'Host'."""
    conn = _Conn(user_id="usr_9", name="")
    created = await _create(svc, conn)
    assert created["state"]["you"]["name"] == "usr_9"


class TestDisabledService:
    """C1: WsConnectionManager.subscribe_to_bus discovers get_ws_handlers()
    exactly once at startup. If it returned {} while disabled, a service
    enabled later via Settings would never get its RPC surface wired up
    without a full process restart, and toggling off wouldn't close it
    either (the manager's cached dict never re-consults get_ws_handlers()).
    Both problems are fixed by registering unconditionally and gating each
    handler individually via _disabled_err()."""

    @pytest.fixture
    async def disabled_svc(self) -> MafiaService:
        service = MafiaService()
        service._config = {"enabled": False}
        await service.on_config_changed(service._config)
        service._resolver = _FakeResolver()
        service._enabled = False
        return service

    async def test_handlers_registered_even_while_disabled(
        self, disabled_svc: MafiaService
    ) -> None:
        handlers = disabled_svc.get_ws_handlers()
        assert handlers  # non-empty — discoverable at boot regardless of enabled state
        assert "mafia.game.create" in handlers
        assert "mafia.game.join" in handlers

    async def test_call_while_disabled_returns_403(self, disabled_svc: MafiaService) -> None:
        resp = await disabled_svc._ws_game_create(_Conn(), {"id": "r1", "theme_key": "camping"})
        assert resp["type"] == "gilbert.error"
        assert resp["code"] == 403
        assert "disabled" in resp["error"].lower()

    async def test_every_registered_handler_rejects_while_disabled(
        self, disabled_svc: MafiaService
    ) -> None:
        """Every frame type registered by get_ws_handlers() must actually
        honor the disabled gate — not just the ones exercised elsewhere."""
        for frame_type, handler in disabled_svc.get_ws_handlers().items():
            resp = await handler(_Conn(), {"id": "x", "game_id": "nope"})
            assert resp["type"] == "gilbert.error", f"{frame_type} did not reject: {resp}"
            assert resp["code"] == 403, f"{frame_type} did not 403: {resp}"

    async def test_toggling_back_on_restores_normal_behavior(
        self, disabled_svc: MafiaService
    ) -> None:
        disabled_svc._enabled = True
        created = await _create(disabled_svc, _Conn())
        assert created["join_code"]
