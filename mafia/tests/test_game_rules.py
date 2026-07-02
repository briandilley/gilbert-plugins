from __future__ import annotations

import random

import pytest
from gilbert_plugin_mafia.game import Character, GameError, MafiaGame, Phase


def _started(n: int, seed: int = 42) -> MafiaGame:
    g = MafiaGame(host_user_id="usr_1", host_name="Cam")
    for i in range(n):
        g.add_player(f"P{i}", user_id="usr_1" if i == 0 else "")
    g.assign_characters(random.Random(seed))
    g.begin_night()
    return g


def _pid(g: MafiaGame, character: Character, index: int = 0) -> str:
    return [p for p in g.players.values() if p.character is character][index].player_id


def _citizen(g: MafiaGame, index: int = 0) -> str:
    return _pid(g, Character.CITIZEN, index)


class TestKillerDuo:
    def test_single_killer_confirms_instantly(self) -> None:
        g = _started(6)
        assert g.killer_act(_pid(g, Character.KILLER), _citizen(g)) == "confirmed"
        assert g.kill_target == _citizen(g)

    def test_duo_propose_then_confirm(self) -> None:
        g = _started(8)
        k1, k2 = _pid(g, Character.KILLER, 0), _pid(g, Character.KILLER, 1)
        target = _citizen(g)
        assert g.killer_act(k1, target) == "proposed"
        assert g.kill_target is None
        with pytest.raises(GameError):
            g.killer_act(k2, _citizen(g, 1))  # must confirm the proposal
        assert g.killer_act(k2, target) == "confirmed"
        assert g.kill_target == target

    def test_proposer_cannot_confirm_own_proposal(self) -> None:
        g = _started(8)
        k1 = _pid(g, Character.KILLER, 0)
        g.killer_act(k1, _citizen(g))
        with pytest.raises(GameError):
            g.killer_act(k1, _citizen(g))

    def test_killers_cannot_target_killers(self) -> None:
        g = _started(8)
        k1, k2 = _pid(g, Character.KILLER, 0), _pid(g, Character.KILLER, 1)
        with pytest.raises(GameError):
            g.killer_act(k1, k2)

    def test_non_killer_cannot_act(self) -> None:
        g = _started(6)
        with pytest.raises(GameError):
            g.killer_act(_citizen(g), _citizen(g, 1))


class TestDoctorDetective:
    def test_doctor_can_self_save(self) -> None:
        g = _started(6)
        doc = _pid(g, Character.DOCTOR)
        g.doctor_act(doc, doc)
        assert g.save_target == doc

    def test_detective_verdict(self) -> None:
        g = _started(7)
        det = _pid(g, Character.DETECTIVE)
        assert g.detective_act(det, _pid(g, Character.KILLER)) is True
        assert g.detective_act(det, _citizen(g)) is False

    def test_detective_cannot_check_self(self) -> None:
        g = _started(7)
        det = _pid(g, Character.DETECTIVE)
        with pytest.raises(GameError):
            g.detective_act(det, det)


class TestNightResolution:
    def test_kill_lands(self) -> None:
        g = _started(6)
        victim = _citizen(g)
        g.killer_act(_pid(g, Character.KILLER), victim)
        died = g.resolve_night()
        assert died is not None and died.player_id == victim
        assert not g.players[victim].alive

    def test_doctor_save_blocks_kill(self) -> None:
        g = _started(6)
        victim = _citizen(g)
        g.killer_act(_pid(g, Character.KILLER), victim)
        g.doctor_act(_pid(g, Character.DOCTOR), victim)
        assert g.resolve_night() is None
        assert g.players[victim].alive

    def test_no_kill_no_victim(self) -> None:
        g = _started(6)
        assert g.resolve_night() is None


class TestVoting:
    def _in_day(self, n: int) -> MafiaGame:
        g = _started(n)
        g.phase = Phase.DAY
        return g

    def test_majority_math(self) -> None:
        g = self._in_day(7)
        assert g.majority_needed() == 4

    def test_majority_target_and_change_and_abstain(self) -> None:
        g = self._in_day(4)  # majority = 3
        pids = [p.player_id for p in g.alive_players()]
        g.cast_vote(pids[0], pids[3])
        g.cast_vote(pids[1], pids[3])
        g.cast_vote(pids[2], "abstain")
        assert g.majority_target() is None
        g.cast_vote(pids[2], pids[3])  # changed vote
        target = g.majority_target()
        assert target is not None and target.player_id == pids[3]

    def test_dead_cannot_vote_or_be_target(self) -> None:
        g = self._in_day(5)
        pids = [p.player_id for p in g.alive_players()]
        g.eliminate(pids[4])
        with pytest.raises(GameError):
            g.cast_vote(pids[4], pids[0])
        with pytest.raises(GameError):
            g.cast_vote(pids[0], pids[4])


class TestPurgeReferences:
    def test_removed_proposer_clears_proposal_pair(self) -> None:
        g = _started(8)  # killer duo
        k1 = _pid(g, Character.KILLER, 0)
        g.killer_act(k1, _citizen(g))
        g.purge_references(k1)
        assert g.kill_proposal is None
        assert g.kill_proposed_by is None

    def test_removed_proposal_target_clears_proposal_pair(self) -> None:
        g = _started(8)
        k1, target = _pid(g, Character.KILLER, 0), _citizen(g)
        g.killer_act(k1, target)
        g.purge_references(target)
        assert g.kill_proposal is None
        assert g.kill_proposed_by is None

    def test_removed_kill_target_cleared(self) -> None:
        g = _started(6)  # single killer confirms instantly
        target = _citizen(g)
        g.killer_act(_pid(g, Character.KILLER), target)
        g.purge_references(target)
        assert g.kill_target is None

    def test_removed_save_target_cleared(self) -> None:
        g = _started(6)
        saved = _citizen(g)
        g.doctor_act(_pid(g, Character.DOCTOR), saved)
        g.purge_references(saved)
        assert g.save_target is None

    def test_votes_by_and_for_removed_player_stripped(self) -> None:
        g = _started(6)
        g.phase = Phase.DAY
        pids = [p.player_id for p in g.alive_players()]
        removed = pids[0]
        g.cast_vote(removed, pids[1])  # vote BY the removed player
        g.cast_vote(pids[1], removed)  # vote FOR the removed player
        g.cast_vote(pids[2], pids[3])  # unrelated vote
        g.purge_references(removed)
        assert removed not in g.votes
        assert removed not in g.votes.values()
        assert g.votes == {pids[2]: pids[3]}

    def test_unrelated_state_untouched(self) -> None:
        g = _started(8)
        k1, target = _pid(g, Character.KILLER, 0), _citizen(g)
        g.killer_act(k1, target)
        g.doctor_act(_pid(g, Character.DOCTOR), _citizen(g, 1))
        g.purge_references(_citizen(g, 2))  # bystander with no references
        assert g.kill_proposal == target
        assert g.kill_proposed_by == k1
        assert g.save_target == _citizen(g, 1)


class TestWinConditions:
    def test_citizens_win_when_killers_gone(self) -> None:
        g = _started(6)
        for k in g.killers():
            g.eliminate(k.player_id)
        assert g.check_winner() == "citizens"

    def test_killers_win_at_parity(self) -> None:
        g = _started(8)  # 2 killers, 6 others
        others = [p for p in g.alive_players() if p.character is not Character.KILLER]
        for p in others[:3]:
            g.eliminate(p.player_id)
        assert g.check_winner() == ""  # 2 killers vs 3 others → continue
        g.eliminate(others[3].player_id)
        assert g.check_winner() == "killers"  # 2 killers vs 2 others → parity

    def test_game_continues_1v2(self) -> None:
        g = _started(6)  # 1 killer
        others = [p for p in g.alive_players() if p.character is not Character.KILLER]
        for p in others[:-2]:
            g.eliminate(p.player_id)
        assert g.check_winner() == ""  # 1 killer vs 2 others
