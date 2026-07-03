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


def test_death_hides_character() -> None:
    """A death reveals *that* a player died, never *what* they were — roles
    stay secret until the game ends."""
    g = _started(8)
    victim = _by_char(g, Character.CITIZEN)
    g.eliminate(victim.player_id)
    state = public_state(g)
    entry = next(p for p in state["players"] if p["player_id"] == victim.player_id)
    assert entry["alive"] is False
    assert entry["character"] is None


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


def test_awaiting_flags_are_simultaneous() -> None:
    g = _started(7)
    killer = _by_char(g, Character.KILLER)
    doctor = _by_char(g, Character.DOCTOR)
    detective = _by_char(g, Character.DETECTIVE)
    citizen = _by_char(g, Character.CITIZEN)
    # NIGHT: everyone owes an action at the same time
    assert state_for(g, killer.player_id)["you"]["awaiting"] == "kill"
    assert state_for(g, doctor.player_id)["you"]["awaiting"] == "save"
    assert state_for(g, detective.player_id)["you"]["awaiting"] == "check"
    assert state_for(g, citizen.player_id)["you"]["awaiting"] == "ready"
    # after submitting, they're done and waiting on the rest
    g.doctor_act(doctor.player_id, doctor.player_id)
    you = state_for(g, doctor.player_id)["you"]
    assert you["awaiting"] is None and you["submitted"] is True
    # DAY: no night action owed
    g.phase = Phase.DAY
    assert state_for(g, killer.player_id)["you"]["awaiting"] is None


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


def test_killer_picks_visible_only_to_the_duo() -> None:
    g = _started(8)
    k1 = _by_char(g, Character.KILLER, 0)
    k2 = _by_char(g, Character.KILLER, 1)
    target = _by_char(g, Character.CITIZEN, 0)
    g.killer_pick(k1.player_id, target.player_id)
    # k1 sees their own pick; k2 sees the partner's live pick
    assert state_for(g, k1.player_id)["you"]["your_night_pick"] == target.player_id
    partner_view = state_for(g, k2.player_id)["you"]["partner_pick"]
    assert partner_view is not None and partner_view["target_id"] == target.player_id
    # non-killers never see kill picks
    doctor = _by_char(g, Character.DOCTOR)
    detective = _by_char(g, Character.DETECTIVE)
    bystander = _by_char(g, Character.CITIZEN, 1)
    for pid in (doctor.player_id, detective.player_id, bystander.player_id):
        you = state_for(g, pid)["you"]
        assert you["partner_pick"] is None and you["your_night_pick"] is None
    # partner agrees → kill locks, both count as submitted
    g.killer_pick(k2.player_id, target.player_id)
    k1_you = state_for(g, k1.player_id)["you"]
    assert k1_you["kill_locked"] is True and k1_you["submitted"] is True


def test_votes_and_majority_gated_to_day_phase() -> None:
    g = _started(8)
    g.votes["someone"] = "target"  # bypass cast_vote to isolate the view gate
    state = public_state(g)
    assert state["votes"] == {}
    assert state["majority_needed"] == 0
    g.phase = Phase.DAY
    state = public_state(g)
    assert state["votes"] == {"someone": "target"}
    assert state["majority_needed"] == g.majority_needed()


def test_game_over_reveals_living_characters() -> None:
    g = _started(8)
    g.winner = "citizens"
    state = public_state(g)
    assert all(p["alive"] for p in state["players"])
    assert all(p["character"] is not None for p in state["players"])
