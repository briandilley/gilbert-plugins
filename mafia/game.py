"""Pure Mafia game state and rules — no I/O, no Gilbert imports.

Vocabulary per std-plugins/CONTEXT.md (Games): Player, Character, Host,
Ghost, Night/Day, Theme, Join code. Rules were locked in the design
grilling (see docs/plans/2026-07-01-mafia-game.md in the core repo).
"""

from __future__ import annotations

import random
import secrets
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

MIN_PLAYERS = 4
DETECTIVE_MIN_PLAYERS = 7
SECOND_KILLER_MIN_PLAYERS = 8

_JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

THEME_SURPRISE = "surprise"
THEME_PRESETS: list[tuple[str, str]] = [
    ("camping", "A camping trip deep in the mountain woods"),
    ("mansion", "A storm-locked haunted mansion"),
    ("cruise", "A 1920s transatlantic cruise ship"),
    ("space", "A remote deep-space mining station"),
    ("western", "A dusty frontier town in the Old West"),
]


class GameError(Exception):
    """A rule violation with a player-facing message."""


class Character(StrEnum):
    CITIZEN = "citizen"
    KILLER = "killer"
    DOCTOR = "doctor"
    DETECTIVE = "detective"


class Phase(StrEnum):
    LOBBY = "lobby"
    NIGHT_KILLERS = "night_killers"
    NIGHT_DOCTOR = "night_doctor"
    NIGHT_DETECTIVE = "night_detective"
    DAWN = "dawn"
    DAY = "day"
    DUSK = "dusk"
    ENDED = "ended"


def characters_for(count: int) -> list[Character]:
    """The locked role matrix: killer+doctor always, detective at 7+, 2nd killer at 8+."""
    if count < MIN_PLAYERS:
        raise ValueError(f"Mafia needs at least {MIN_PLAYERS} players")
    chars = [Character.KILLER, Character.DOCTOR]
    if count >= DETECTIVE_MIN_PLAYERS:
        chars.append(Character.DETECTIVE)
    if count >= SECOND_KILLER_MIN_PLAYERS:
        chars.append(Character.KILLER)
    chars.extend([Character.CITIZEN] * (count - len(chars)))
    return chars


def _make_join_code() -> str:
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(6))


@dataclass
class Player:
    player_id: str
    name: str
    token: str
    user_id: str = ""  # real account id; "" for code-joined Players
    character: Character = Character.CITIZEN
    alive: bool = True


@dataclass
class MafiaGame:
    host_user_id: str
    host_name: str
    theme: str = ""       # resolved description text fed to the Narrator
    theme_key: str = ""   # preset key, "custom", or "surprise"
    game_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    join_code: str = field(default_factory=_make_join_code)
    phase: Phase = Phase.LOBBY
    night: int = 0
    players: dict[str, Player] = field(default_factory=dict)
    # night state (cleared by begin_night in Task 6)
    kill_proposed_by: str | None = None
    kill_proposal: str | None = None
    kill_target: str | None = None
    save_target: str | None = None
    checks: dict[str, list[str]] = field(default_factory=dict)  # detective pid → checked pids
    votes: dict[str, str] = field(default_factory=dict)  # voter pid → target pid | "abstain"
    story: list[str] = field(default_factory=list)
    winner: str = ""  # "" | "citizens" | "killers" | "aborted"

    def add_player(self, name: str, user_id: str = "") -> Player:
        if self.phase is not Phase.LOBBY:
            raise GameError("The game has already started")
        clean = name.strip()
        if not clean:
            raise GameError("Pick a name first")
        if any(p.name.lower() == clean.lower() for p in self.players.values()):
            raise GameError(f"The name {clean!r} is taken")
        player = Player(
            player_id=uuid.uuid4().hex[:8],
            name=clean,
            token=secrets.token_urlsafe(16),
            user_id=user_id,
        )
        self.players[player.player_id] = player
        return player

    def assign_characters(self, rng: random.Random | None = None) -> None:
        try:
            chars = characters_for(len(self.players))
        except ValueError as exc:
            raise GameError(str(exc)) from exc
        (rng or random).shuffle(chars)
        for player, character in zip(self.players.values(), chars, strict=True):
            player.character = character

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def alive_with(self, character: Character) -> list[Player]:
        return [p for p in self.alive_players() if p.character is character]

    def killers(self) -> list[Player]:
        return [p for p in self.players.values() if p.character is Character.KILLER]

    def player_by_token(self, token: str) -> Player | None:
        for p in self.players.values():
            if p.token and secrets.compare_digest(p.token, token):
                return p
        return None
