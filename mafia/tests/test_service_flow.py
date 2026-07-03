from __future__ import annotations

import asyncio
from typing import Any

import pytest
from gilbert_plugin_mafia.game import Character, MafiaGame, Phase
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


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Resp:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _FakeAI:
    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(self, **kwargs: Any) -> _Resp:
        return _Resp("A story beat.")


class _FakeResolver:
    def get_capability(self, name: str) -> Any:
        return _FakeAI() if name == "ai_chat" else None

    def require_capability(self, name: str) -> Any:
        raise LookupError(name)

    def get_all(self, name: str) -> list[Any]:
        return []


@pytest.fixture
async def svc() -> MafiaService:
    service = MafiaService()
    service._config = {"enabled": True, "nudge_seconds": 9999}
    await service.on_config_changed(service._config)
    service._resolver = _FakeResolver()
    service._enabled = True
    return service


@pytest.fixture
async def table() -> tuple[MafiaService, dict[str, Any], dict[str, _Conn], dict[str, dict[str, Any]]]:
    """A started 4-player game. Returns (svc, created, conns, joins by name)."""
    svc = MafiaService()
    svc._config = {"enabled": True, "nudge_seconds": 9999}
    await svc.on_config_changed(svc._config)
    svc._resolver = _FakeResolver()
    svc._enabled = True

    host_conn = _Conn()
    created = await svc._ws_game_create(host_conn, {"id": "c", "theme_key": "camping"})
    conns: dict[str, _Conn] = {"Cam": host_conn}
    joins: dict[str, dict[str, Any]] = {
        "Cam": {
            "game_id": created["game_id"],
            "player_id": created["player_id"],
            "player_token": created["player_token"],
        }
    }
    for name in ("Ana", "Ben", "Dot"):
        conn = _Conn(user_id="guest", name=name, level=200)
        joined = await svc._ws_game_join(
            conn, {"id": "j", "join_code": created["join_code"], "name": name}
        )
        conns[name] = conn
        joins[name] = joined
    resp = await svc._ws_game_start(host_conn, {"id": "s", "game_id": created["game_id"]})
    assert resp["type"] == "mafia.game.start.result", resp
    yield svc, created, conns, joins
    # Nudge tasks sleep for `nudge_seconds` (9999s in this fixture) and outlive
    # any test that doesn't itself end/abort the game — cancel unconditionally
    # so the event loop doesn't tear down with a pending task (idempotent).
    svc._cancel_nudge(created["game_id"])


def _game(svc: MafiaService, created: dict[str, Any]):
    return svc._games[created["game_id"]]


def _player_named(game: Any, name: str):
    return next(p for p in game.players.values() if p.name == name)


def _by_char(game: Any, c: Character, i: int = 0):
    return [p for p in game.players.values() if p.character is c][i]


def _token(game: Any, joins: dict[str, dict[str, Any]], player: Any) -> str:
    return joins[player.name]["player_token"]


async def _act(svc: MafiaService, game: Any, joins: dict[str, Any], player: Any, target: Any):
    return await svc._ws_night_act(
        None,
        {
            "id": "a",
            "game_id": game.game_id,
            "player_token": _token(game, joins, player),
            "target_id": target.player_id,
        },
    )


async def test_start_assigns_and_enters_night(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    assert game.phase is Phase.NIGHT_KILLERS
    assert game.night == 1  # kill from night 1
    assert len(game.story) >= 1  # intro beat narrated


async def test_start_requires_host(table) -> None:
    svc, created, conns, joins = table
    resp = await svc._ws_game_start(conns["Ana"], {"id": "x", "game_id": created["game_id"]})
    assert resp["type"] == "gilbert.error"


class _SlowFakeAI:
    """Like _FakeAI, but complete_one_shot blocks on an asyncio.Event.

    Lets a test pause a _ws_game_start call mid-flight (after the
    synchronous host/phase/assign_characters section, at the first
    narration await) to exercise the LOBBY→non-LOBBY race window (I1).
    """

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls = 0

    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(self, **kwargs: Any) -> _Resp:
        self.calls += 1
        await self.gate.wait()
        return _Resp("A story beat.")


class _SlowResolver:
    def __init__(self, ai: Any) -> None:
        self._ai = ai

    def get_capability(self, name: str) -> Any:
        return self._ai if name == "ai_chat" else None

    def require_capability(self, name: str) -> Any:
        raise LookupError(name)

    def get_all(self, name: str) -> list[Any]:
        return []


async def _slow_lobby() -> tuple[MafiaService, dict[str, Any], dict[str, _Conn], _SlowFakeAI]:
    """A 4-player lobby (not yet started) wired to a gate-blocked AI."""
    ai = _SlowFakeAI()
    svc = MafiaService()
    svc._config = {"enabled": True, "nudge_seconds": 9999}
    await svc.on_config_changed(svc._config)
    svc._resolver = _SlowResolver(ai)
    svc._enabled = True

    host_conn = _Conn()
    created = await svc._ws_game_create(host_conn, {"id": "c", "theme_key": "camping"})
    conns: dict[str, _Conn] = {"Cam": host_conn}
    for name in ("Ana", "Ben", "Dot"):
        conn = _Conn(user_id="guest", name=name, level=200)
        await svc._ws_game_join(conn, {"id": "j", "join_code": created["join_code"], "name": name})
        conns[name] = conn
    return svc, created, conns, ai


async def test_concurrent_starts_race_only_one_succeeds() -> None:
    """I1: phase must leave LOBBY (and characters must be assigned) before
    the multi-second narration awaits in _ws_game_start, so a second,
    concurrent start can't reshuffle characters or double-run the night."""
    svc, created, conns, ai = await _slow_lobby()
    game = svc._games[created["game_id"]]

    orig_assign = game.assign_characters
    assign_calls = 0

    def counting_assign(*args: Any, **kwargs: Any) -> None:
        nonlocal assign_calls
        assign_calls += 1
        return orig_assign(*args, **kwargs)

    game.assign_characters = counting_assign  # type: ignore[method-assign]

    frame = {"game_id": created["game_id"]}
    t1 = asyncio.create_task(svc._ws_game_start(conns["Cam"], {"id": "s1", **frame}))
    t2 = asyncio.create_task(svc._ws_game_start(conns["Cam"], {"id": "s2", **frame}))

    # Let both tasks run their synchronous prefix (host/phase check,
    # assign_characters, phase flip away from LOBBY) before either narration
    # await resolves. The second task's LOBBY check is synchronous too, so
    # once the first has flipped the phase, the second's error return
    # doesn't need the AI gate at all.
    for _ in range(20):
        await asyncio.sleep(0)

    assert game.phase is not Phase.LOBBY
    assert assign_calls == 1  # characters assigned exactly once

    ai.gate.set()
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)

    results = [r1, r2]
    successes = [r for r in results if r.get("type") == "mafia.game.start.result"]
    errors = [r for r in results if r.get("type") == "gilbert.error"]
    assert len(successes) == 1, results
    assert len(errors) == 1, results
    assert "already started" in errors[0]["error"].lower()
    assert assign_calls == 1  # still exactly one — not reshuffled by the loser
    assert game.night == 1

    svc._cancel_nudge(created["game_id"])


async def test_join_mid_start_is_refused() -> None:
    """I1(b): a join issued while a start is in flight (after the phase has
    left LOBBY but before the game finishes starting) must be rejected."""
    svc, created, conns, ai = await _slow_lobby()
    game = svc._games[created["game_id"]]

    t1 = asyncio.create_task(
        svc._ws_game_start(conns["Cam"], {"id": "s1", "game_id": created["game_id"]})
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert game.phase is not Phase.LOBBY

    late = _Conn(user_id="guest", name="Late", level=200)
    resp = await svc._ws_game_join(
        late, {"id": "j-late", "join_code": created["join_code"], "name": "Late"}
    )
    assert resp["type"] == "gilbert.error"
    assert "already started" in resp["error"].lower()

    ai.gate.set()
    await asyncio.wait_for(t1, timeout=5)
    svc._cancel_nudge(created["game_id"])


async def test_full_night_to_day(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    doctor = _by_char(game, Character.DOCTOR)
    victim = _by_char(game, Character.CITIZEN)

    resp = await _act(svc, game, joins, killer, victim)
    assert resp["type"] == "mafia.night.act.result"
    assert game.phase is Phase.NIGHT_DOCTOR  # 4 players → no detective

    other = next(p for p in game.alive_players() if p.player_id != victim.player_id)
    await _act(svc, game, joins, doctor, other)  # saves the wrong person
    assert game.phase is Phase.DAY
    assert not game.players[victim.player_id].alive
    # dawn narration revealed the character while eyes were closed
    assert len(game.story) >= 3


async def test_vote_majority_eliminates_and_night_falls(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    doctor = _by_char(game, Character.DOCTOR)
    victim = _by_char(game, Character.CITIZEN)
    await _act(svc, game, joins, killer, victim)
    await _act(svc, game, joins, doctor, doctor)  # self-save, kill lands on victim

    alive = game.alive_players()  # 3 alive → majority 2
    target = killer
    voters = [p for p in alive if p.player_id != target.player_id][:2]
    for voter in voters:
        resp = await svc._ws_vote_cast(
            None,
            {
                "id": "v",
                "game_id": game.game_id,
                "player_token": _token(game, joins, voter),
                "target": target.player_id,
            },
        )
        assert resp["type"] == "mafia.vote.cast.result"
    # killer voted out → citizens win, game ends
    assert game.winner == "citizens"
    assert game.phase is Phase.ENDED


async def test_win_removes_game_and_conns_after_final_push(table) -> None:
    """I3: an ended (non-aborted) game must be dropped from the server-side
    registries -- retaining ENDED games forever grows memory unboundedly,
    and a join-code collision with a retained ENDED game can block a new
    lobby from forming. Ghosts keep the final state client-side since the
    winning push already went out before the pop."""
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    doctor = _by_char(game, Character.DOCTOR)
    victim = _by_char(game, Character.CITIZEN)
    await _act(svc, game, joins, killer, victim)
    await _act(svc, game, joins, doctor, doctor)  # self-save, kill lands on victim

    alive = game.alive_players()  # 3 alive → majority 2
    voters = [p for p in alive if p.player_id != killer.player_id][:2]
    for voter in voters:
        resp = await svc._ws_vote_cast(
            None,
            {
                "id": "v",
                "game_id": game.game_id,
                "player_token": _token(game, joins, voter),
                "target": killer.player_id,
            },
        )
        assert resp["type"] == "mafia.vote.cast.result"

    # `game` was captured BEFORE the winning action -- the reference stays
    # valid even though the service has since dropped it from its dicts.
    assert game.winner == "citizens"
    assert game.phase is Phase.ENDED
    assert created["game_id"] not in svc._games
    assert created["game_id"] not in svc._conns

    for name, conn in conns.items():
        final_pushes = [
            m for m in conn.sent if m["type"] == "mafia.state" and m["state"].get("winner")
        ]
        assert final_pushes, f"{name} never received the final winning state"


async def test_host_end_day_without_majority(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    doctor = _by_char(game, Character.DOCTOR)
    victim = _by_char(game, Character.CITIZEN)
    await _act(svc, game, joins, killer, victim)
    await _act(svc, game, joins, doctor, victim)  # doctor saves the victim → nobody dies
    assert game.phase is Phase.DAY
    assert game.players[victim.player_id].alive
    resp = await svc._ws_host_end_day(conns["Cam"], {"id": "e", "game_id": game.game_id})
    assert resp["type"] == "mafia.host.end_day.result"
    assert game.phase is Phase.NIGHT_KILLERS
    assert game.night == 2


async def test_host_skip_forfeits_kill(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    resp = await svc._ws_host_skip(conns["Cam"], {"id": "k", "game_id": game.game_id})
    assert resp["type"] == "mafia.host.skip_phase.result"
    assert game.phase is not Phase.NIGHT_KILLERS  # advanced past killers


async def test_host_abort(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    resp = await svc._ws_host_abort(conns["Cam"], {"id": "q", "game_id": game.game_id})
    assert resp["type"] == "mafia.host.abort.result"
    assert game.winner == "aborted"
    assert created["game_id"] not in svc._games


async def _remove(svc: MafiaService, conns: dict[str, _Conn], game: Any, player: Any):
    return await svc._ws_host_remove(
        conns["Cam"], {"id": "r", "game_id": game.game_id, "player_id": player.player_id}
    )


async def test_host_remove_bystander_keeps_night_phase(table) -> None:
    """Removing a citizen during NIGHT_KILLERS must not forfeit the kill."""
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    bystander = _by_char(game, Character.CITIZEN)
    other = _by_char(game, Character.CITIZEN, 1)

    resp = await _remove(svc, conns, game, bystander)
    assert resp["type"] == "mafia.host.remove_player.result"
    assert game.phase is Phase.NIGHT_KILLERS  # killer is still the awaited actor
    # the killer can still act
    resp = await _act(svc, game, joins, killer, other)
    assert resp["type"] == "mafia.night.act.result"
    assert game.phase is Phase.NIGHT_DOCTOR


async def test_host_remove_awaited_doctor_advances_and_purges_kill(table) -> None:
    """Removing the awaited doctor advances the night; their stale kill_target is purged."""
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    doctor = _by_char(game, Character.DOCTOR)

    await _act(svc, game, joins, killer, doctor)  # killer targets the doctor
    assert game.phase is Phase.NIGHT_DOCTOR

    resp = await _remove(svc, conns, game, doctor)
    assert resp["type"] == "mafia.host.remove_player.result"
    # doctor was the awaited actor → night advanced (4 players: no detective → dawn → day)
    assert game.phase is Phase.DAY
    # the pending kill pointed at the removed doctor and was purged — nobody double-dies
    assert game.kill_target is None


async def test_host_remove_sole_killer_ends_game(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    resp = await _remove(svc, conns, game, killer)
    assert resp["type"] == "mafia.host.remove_player.result"
    assert game.winner == "citizens"
    assert game.phase is Phase.ENDED


async def test_host_remove_purges_votes_for_target(table) -> None:
    """Stale votes for a removed player must not later 'eliminate' the dead player."""
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    doctor = _by_char(game, Character.DOCTOR)
    victim = _by_char(game, Character.CITIZEN)
    other = _by_char(game, Character.CITIZEN, 1)

    await _act(svc, game, joins, killer, victim)
    await _act(svc, game, joins, doctor, victim)  # save → nobody dies, 4 alive in DAY
    assert game.phase is Phase.DAY
    assert game.majority_needed() == 3

    for voter in (killer, doctor):  # 2 votes for victim — below majority of 3
        resp = await svc._ws_vote_cast(
            None,
            {
                "id": "v",
                "game_id": game.game_id,
                "player_token": _token(game, joins, voter),
                "target": victim.player_id,
            },
        )
        assert resp["type"] == "mafia.vote.cast.result"

    await _remove(svc, conns, game, victim)  # 3 alive → majority drops to 2
    assert victim.player_id not in game.votes.values()

    # a subsequent vote must not resolve a majority onto the already-dead victim
    resp = await svc._ws_vote_cast(
        None,
        {
            "id": "v2",
            "game_id": game.game_id,
            "player_token": _token(game, joins, other),
            "target": "abstain",
        },
    )
    assert resp["type"] == "mafia.vote.cast.result"
    assert game.phase is Phase.DAY
    assert game.winner == ""


async def test_host_remove_resolves_pending_majority_in_day(svc: MafiaService) -> None:
    """S9: removing bystanders during DAY can drop the alive count enough
    that an already-cast vote tally now clears the (now-lower) majority
    threshold. That must resolve immediately -- not wait for a new vote."""
    host = _Conn()
    game = MafiaGame(host_user_id=host.user_ctx.user_id, host_name="Cam")
    for name in ("Cam", "Bob", "X1", "X2", "X3"):
        game.add_player(name, user_id=host.user_ctx.user_id if name == "Cam" else "")
    # Manually assign a killer who isn't touched by this scenario, so
    # check_winner() doesn't declare a premature win the moment any
    # player is removed (default character is CITIZEN for everyone).
    cam = next(p for p in game.players.values() if p.name == "Cam")
    cam.character = Character.KILLER
    bob = next(p for p in game.players.values() if p.name == "Bob")
    x1 = next(p for p in game.players.values() if p.name == "X1")
    x2 = next(p for p in game.players.values() if p.name == "X2")
    x3 = next(p for p in game.players.values() if p.name == "X3")
    game.phase = Phase.DAY
    game.votes = {cam.player_id: bob.player_id, x1.player_id: bob.player_id}
    svc._games[game.game_id] = game

    assert game.majority_needed() == 3  # 5 alive
    resp = await svc._ws_host_remove(
        host, {"id": "r1", "game_id": game.game_id, "player_id": x2.player_id}
    )
    assert resp["type"] == "mafia.host.remove_player.result"
    assert bob.alive  # 4 alive → majority still 3, 2 votes isn't enough yet

    resp = await svc._ws_host_remove(
        host, {"id": "r2", "game_id": game.game_id, "player_id": x3.player_id}
    )
    assert resp["type"] == "mafia.host.remove_player.result"
    # 3 alive → majority drops to 2, and Bob already has 2 votes: resolved
    # without any new mafia.vote.cast call.
    assert not bob.alive


async def test_secret_push_targeting(table) -> None:
    """The killer's awaiting flag reaches only the killer's connection."""
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    for name, conn in conns.items():
        player = _player_named(game, name)
        states = [m["state"] for m in conn.sent if m.get("type") == "mafia.state"]
        assert states, f"{name} got no pushes"
        last = states[-1]
        assert last["you"]["player_id"] == player.player_id
        if player.player_id != killer.player_id:
            others = [p for p in last["players"] if p["player_id"] != player.player_id]
            assert all(p["character"] is None for p in others if p["alive"])
