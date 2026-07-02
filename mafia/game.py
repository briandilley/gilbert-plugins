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
from typing import Any

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

    # --- night ---

    def begin_night(self) -> None:
        self.night += 1
        self.kill_proposed_by = None
        self.kill_proposal = None
        self.kill_target = None
        self.save_target = None
        self.votes = {}
        self.phase = Phase.NIGHT_KILLERS

    def _living(self, player_id: str) -> Player:
        player = self.players.get(player_id)
        if player is None or not player.alive:
            raise GameError("That player is not in the game (or not alive)")
        return player

    def killer_act(self, player_id: str, target_id: str) -> str:
        actor = self._living(player_id)
        if actor.character is not Character.KILLER:
            raise GameError("You are not a killer")
        target = self._living(target_id)
        if target.character is Character.KILLER:
            raise GameError("You cannot target a fellow killer")
        living_killers = self.alive_with(Character.KILLER)
        if len(living_killers) == 1:
            self.kill_target = target.player_id
            return "confirmed"
        if self.kill_proposal is None:
            self.kill_proposal = target.player_id
            self.kill_proposed_by = actor.player_id
            return "proposed"
        if actor.player_id == self.kill_proposed_by:
            raise GameError("Wait for your partner to confirm")
        if target.player_id != self.kill_proposal:
            proposed = self.players[self.kill_proposal].name
            raise GameError(f"Your partner chose {proposed} — tap them to confirm")
        self.kill_target = target.player_id
        return "confirmed"

    def doctor_act(self, player_id: str, target_id: str) -> None:
        actor = self._living(player_id)
        if actor.character is not Character.DOCTOR:
            raise GameError("You are not the doctor")
        self.save_target = self._living(target_id).player_id

    def detective_act(self, player_id: str, target_id: str) -> bool:
        actor = self._living(player_id)
        if actor.character is not Character.DETECTIVE:
            raise GameError("You are not the detective")
        if target_id == player_id:
            raise GameError("You already know about yourself")
        target = self._living(target_id)
        self.checks.setdefault(actor.player_id, []).append(target.player_id)
        return target.character is Character.KILLER

    def resolve_night(self) -> Player | None:
        if self.kill_target is None or self.kill_target == self.save_target:
            return None
        victim = self.players[self.kill_target]
        victim.alive = False
        return victim

    # --- day ---

    def cast_vote(self, voter_id: str, target: str | None) -> None:
        if self.phase is not Phase.DAY:
            raise GameError("There is no vote right now")
        self._living(voter_id)
        if target is None:
            self.votes.pop(voter_id, None)
            return
        if target != "abstain":
            self._living(target)
        self.votes[voter_id] = target

    def majority_needed(self) -> int:
        return len(self.alive_players()) // 2 + 1

    def tally(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for target in self.votes.values():
            counts[target] = counts.get(target, 0) + 1
        return counts

    def majority_target(self) -> Player | None:
        needed = self.majority_needed()
        for target, count in self.tally().items():
            if target != "abstain" and count >= needed:
                return self.players[target]
        return None

    def eliminate(self, player_id: str) -> Player:
        player = self.players[player_id]
        player.alive = False
        return player

    def purge_references(self, player_id: str) -> None:
        """Drop night picks and votes that point at a removed player."""
        if self.kill_proposal == player_id or self.kill_proposed_by == player_id:
            self.kill_proposal = None
            self.kill_proposed_by = None
        if self.kill_target == player_id:
            self.kill_target = None
        if self.save_target == player_id:
            self.save_target = None
        self.votes.pop(player_id, None)
        self.votes = {v: t for v, t in self.votes.items() if t != player_id}

    # --- outcome ---

    def check_winner(self) -> str:
        living = self.alive_players()
        killers = [p for p in living if p.character is Character.KILLER]
        others = [p for p in living if p.character is not Character.KILLER]
        if not killers:
            return "citizens"
        if len(killers) >= len(others):
            return "killers"
        return ""


def _character_public(game: MafiaGame, player: Player) -> str | None:
    """Reveal-on-death: characters are public once dead, or when the game is over."""
    if not player.alive or game.winner:
        return str(player.character)
    return None


def public_state(game: MafiaGame) -> dict[str, Any]:
    in_day = game.phase is Phase.DAY
    return {
        "game_id": game.game_id,
        "phase": str(game.phase),
        "night": game.night,
        "theme_key": game.theme_key,
        "join_code": game.join_code if game.phase is Phase.LOBBY else "",
        "players": [
            {
                "player_id": p.player_id,
                "name": p.name,
                "alive": p.alive,
                "is_host": p.user_id == game.host_user_id,
                "character": _character_public(game, p),
            }
            for p in game.players.values()
        ],
        "story": list(game.story),
        "votes": dict(game.votes) if in_day else {},
        "majority_needed": game.majority_needed() if in_day else 0,
        "winner": game.winner,
    }


def _awaiting_for(game: MafiaGame, player: Player) -> str | None:
    if not player.alive:
        return None
    if game.phase is Phase.NIGHT_KILLERS and player.character is Character.KILLER:
        if game.kill_target is not None:
            return None
        if game.kill_proposal is not None:
            return None if player.player_id == game.kill_proposed_by else "kill_confirm"
        return "kill"
    if game.phase is Phase.NIGHT_DOCTOR and player.character is Character.DOCTOR:
        return "save" if game.save_target is None else None
    if game.phase is Phase.NIGHT_DETECTIVE and player.character is Character.DETECTIVE:
        return "check"
    return None


def state_for(game: MafiaGame, player_id: str) -> dict[str, Any]:
    state = public_state(game)
    player = game.players[player_id]
    assigned = game.phase is not Phase.LOBBY
    partner_name: str | None = None
    if assigned and player.character is Character.KILLER:
        partners = [k for k in game.killers() if k.player_id != player.player_id and k.alive]
        partner_name = partners[0].name if partners else None
    proposal: dict[str, str] | None = None
    if (
        player.character is Character.KILLER
        and game.kill_proposal is not None
        and game.kill_target is None
    ):
        target = game.players[game.kill_proposal]
        proposal = {"target_id": target.player_id, "target_name": target.name}
    check_results = [
        {
            "player_id": pid,
            "name": game.players[pid].name,
            "is_killer": game.players[pid].character is Character.KILLER,
        }
        for pid in game.checks.get(player.player_id, [])
    ]
    ghost: dict[str, Any] | None = None
    if not player.alive or game.winner:
        ghost = {
            "characters": {p.player_id: str(p.character) for p in game.players.values()}
        }
    state["you"] = {
        "player_id": player.player_id,
        "name": player.name,
        "alive": player.alive,
        "is_host": player.user_id == game.host_user_id,
        "character": str(player.character) if assigned else None,
        "partner_name": partner_name,
        "awaiting": _awaiting_for(game, player),
        "kill_proposal": proposal,
        "check_results": check_results,
        "ghost": ghost,
    }
    return state
