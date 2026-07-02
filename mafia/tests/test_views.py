from __future__ import annotations

import random

from gilbert_plugin_mafia.game import (
    Character,
    MafiaGame,
    Phase,
    public_state,
    state_for,
)


def _started(n: int, seed: int = 42) -> MafiaGame:
    g = MafiaGame(host_user_id="usr_1", host_name="Cam")
    for i in range(n):
        g.add_player(f"P{i}", user_id="usr_1" if i == 0 else "")
    g.assign_characters(random.Random(seed))
    g.begin_night()
    return g


def _by_char(g: MafiaGame, c: Character, i: int = 0):
    return [p for p in g.players.values() if p.character is c][i]


def test_public_state_hides_living_characters() -> None:
    g = _started(8)
    state = public_state(g)
    assert all(p["character"] is None for p in state["players"])
    assert state["join_code"] == ""  # not in lobby


def test_death_reveals_character_publicly() -> None:
    g = _started(8)
    victim = _by_char(g, Character.CITIZEN)
    g.eliminate(victim.player_id)
    state = public_state(g)
    entry = next(p for p in state["players"] if p["player_id"] == victim.player_id)
    assert entry["character"] == "citizen"


def test_citizen_sees_no_secrets() -> None:
    g = _started(8)
    citizen = _by_char(g, Character.CITIZEN)
    # The word "killer" may appear only as the citizen's own (None) or phase names —
    # assert the killer's player_id is never associated with a character string.
    state = state_for(g, citizen.player_id)
    others = [p for p in state["players"] if p["player_id"] != citizen.player_id]
    assert all(p["character"] is None for p in others)
    assert state["you"]["character"] == "citizen"
    assert state["you"]["ghost"] is None
    assert state["you"]["partner_name"] is None


def test_killers_see_each_other() -> None:
    g = _started(8)
    k1, k2 = _by_char(g, Character.KILLER, 0), _by_char(g, Character.KILLER, 1)
    assert state_for(g, k1.player_id)["you"]["partner_name"] == k2.name


def test_awaiting_flags_follow_phase() -> None:
    g = _started(7)
    killer = _by_char(g, Character.KILLER)
    doctor = _by_char(g, Character.DOCTOR)
    assert state_for(g, killer.player_id)["you"]["awaiting"] == "kill"
    assert state_for(g, doctor.player_id)["you"]["awaiting"] is None
    g.phase = Phase.NIGHT_DOCTOR
    assert state_for(g, doctor.player_id)["you"]["awaiting"] == "save"


def test_detective_results_private() -> None:
    g = _started(7)
    det = _by_char(g, Character.DETECTIVE)
    killer = _by_char(g, Character.KILLER)
    g.detective_act(det.player_id, killer.player_id)
    mine = state_for(g, det.player_id)["you"]["check_results"]
    assert mine == [
        {"player_id": killer.player_id, "name": killer.name, "is_killer": True}
    ]
    citizen = _by_char(g, Character.CITIZEN)
    assert state_for(g, citizen.player_id)["you"]["check_results"] == []


def test_ghost_sees_everything() -> None:
    g = _started(8)
    citizen = _by_char(g, Character.CITIZEN)
    g.eliminate(citizen.player_id)
    ghost = state_for(g, citizen.player_id)["you"]["ghost"]
    assert ghost is not None
    assert set(ghost["characters"].values()) >= {"killer", "doctor"}
