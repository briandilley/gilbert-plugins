from __future__ import annotations

import random

import pytest
from gilbert_plugin_mafia.game import (
    MAX_NAME_LENGTH,
    MAX_PLAYERS,
    Character,
    GameError,
    MafiaGame,
    Phase,
    characters_for,
)


def _game(n: int) -> MafiaGame:
    g = MafiaGame(host_user_id="usr_1", host_name="Cam")
    for i in range(n):
        g.add_player(f"P{i}", user_id="usr_1" if i == 0 else "")
    return g


class TestCharacterMatrix:
    def test_minimum_four(self) -> None:
        with pytest.raises(ValueError):
            characters_for(3)

    @pytest.mark.parametrize(
        ("count", "killers", "doctors", "detectives"),
        [(4, 1, 1, 0), (6, 1, 1, 0), (7, 1, 1, 1), (8, 2, 1, 1), (10, 2, 1, 1)],
    )
    def test_matrix(self, count: int, killers: int, doctors: int, detectives: int) -> None:
        chars = characters_for(count)
        assert len(chars) == count
        assert chars.count(Character.KILLER) == killers
        assert chars.count(Character.DOCTOR) == doctors
        assert chars.count(Character.DETECTIVE) == detectives
        assert chars.count(Character.CITIZEN) == count - killers - doctors - detectives


class TestLobby:
    def test_join_code_format(self) -> None:
        g = _game(0)
        assert len(g.join_code) == 6
        assert g.join_code.isalnum()

    def test_add_player_unique_names(self) -> None:
        g = _game(1)
        with pytest.raises(GameError):
            g.add_player("P0")

    def test_no_join_after_start(self) -> None:
        g = _game(4)
        g.assign_characters(random.Random(42))
        g.phase = Phase.NIGHT_KILLERS
        with pytest.raises(GameError):
            g.add_player("Late")

    def test_assign_requires_minimum(self) -> None:
        g = _game(3)
        with pytest.raises(GameError):
            g.assign_characters(random.Random(42))

    def test_assign_is_seeded_and_complete(self) -> None:
        g = _game(8)
        g.assign_characters(random.Random(42))
        assigned = [p.character for p in g.players.values()]
        assert assigned.count(Character.KILLER) == 2
        assert len(g.killers()) == 2

    def test_player_tokens_unique_and_lookup(self) -> None:
        g = _game(4)
        tokens = {p.token for p in g.players.values()}
        assert len(tokens) == 4
        some = next(iter(g.players.values()))
        assert g.player_by_token(some.token) is some
        assert g.player_by_token("nope") is None

    def test_add_player_unique_names_case_insensitive(self) -> None:
        """S3: a duplicate name differing only by case must be rejected."""
        g = _game(1)
        with pytest.raises(GameError):
            g.add_player("p0")

    def test_add_player_rejects_whitespace_only_name(self) -> None:
        """S3: an empty or whitespace-only name must be rejected."""
        g = _game(0)
        with pytest.raises(GameError):
            g.add_player("   ")

    def test_add_player_rejects_empty_name(self) -> None:
        g = _game(0)
        with pytest.raises(GameError):
            g.add_player("")


class TestAddPlayerHardening:
    """I2: guest join-flood hardening on add_player."""

    def test_name_too_long_rejected(self) -> None:
        g = _game(0)
        with pytest.raises(GameError, match="too long"):
            g.add_player("x" * (MAX_NAME_LENGTH + 1))

    def test_name_at_max_length_accepted(self) -> None:
        g = _game(0)
        player = g.add_player("x" * MAX_NAME_LENGTH)
        assert player.name == "x" * MAX_NAME_LENGTH

    def test_control_characters_stripped_from_name(self) -> None:
        g = _game(0)
        player = g.add_player("Ann\x00a\x07\x1b")
        assert player.name == "Anna"

    def test_name_that_is_only_control_characters_rejected(self) -> None:
        g = _game(0)
        with pytest.raises(GameError, match="Pick a name"):
            g.add_player("\x00\x01\x02")

    def test_max_players_enforced(self) -> None:
        g = _game(MAX_PLAYERS)
        with pytest.raises(GameError, match="full"):
            g.add_player("One too many")
