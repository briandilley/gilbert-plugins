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
MAX_PLAYERS = 20
MAX_NAME_LENGTH = 30

_JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# ASCII C0 control characters + DEL — stripped from player-supplied names
# (guest join-flood / display hardening; see add_player()).
_CONTROL_CHARS = "".join(chr(c) for c in range(0x20)) + chr(0x7F)
_CONTROL_CHAR_TABLE = str.maketrans("", "", _CONTROL_CHARS)

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
    # No eyes-closed sequence: at NIGHT every living player submits a choice
    # simultaneously (killers pick, doctor saves, detective checks, citizens
    # ready up). The night resolves once everyone has submitted; the outcome
    # is narrated and play moves straight to DAY.
    LOBBY = "lobby"
    NIGHT = "night"
    DAY = "day"
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
    # Per-game narration output, chosen by the host at create time. These
    # were once service-wide config; narration is spoken in the room the
    # game is played in, so which speakers narrate and how loud belongs to
    # the individual game, not a global setting. ``speaker_names`` of
    # ``None`` (or empty) falls back to the speaker service's default
    # announce speakers; ``volume`` is 0-100.
    speaker_names: list[str] | None = None
    volume: int = 70
    game_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    join_code: str = field(default_factory=_make_join_code)
    phase: Phase = Phase.LOBBY
    night: int = 0
    players: dict[str, Player] = field(default_factory=dict)
    # --- simultaneous-night state (reset each begin_night) ---
    # Each killer's current target pick. Killers see each other's picks live
    # and change until they agree; on agreement the kill locks.
    night_kill_picks: dict[str, str] = field(default_factory=dict)  # killer pid → target pid
    kill_target: str | None = None  # the agreed, locked kill target
    kill_locked: bool = False       # killers have agreed → target is final
    save_target: str | None = None  # doctor's protection pick
    checks: dict[str, list[str]] = field(default_factory=dict)  # detective pid → checked pids
    night_ready: set[str] = field(default_factory=set)  # players who've submitted their action
    votes: dict[str, str] = field(default_factory=dict)  # voter pid → target pid | "abstain"
    story: list[str] = field(default_factory=list)
    winner: str = ""  # "" | "citizens" | "killers" | "aborted"

    def add_player(self, name: str, user_id: str = "") -> Player:
        if self.phase is not Phase.LOBBY:
            raise GameError("The game has already started")
        if len(self.players) >= MAX_PLAYERS:
            raise GameError("The game is full")
        clean = name.translate(_CONTROL_CHAR_TABLE).strip()
        if not clean:
            raise GameError("Pick a name first")
        if len(clean) > MAX_NAME_LENGTH:
            raise GameError("That name is too long")
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

    # --- night (simultaneous) ---

    def begin_night(self) -> None:
        self.night += 1
        self.night_kill_picks = {}
        self.kill_target = None
        self.kill_locked = False
        self.save_target = None
        self.night_ready = set()
        self.votes = {}
        self.phase = Phase.NIGHT

    def _living(self, player_id: str) -> Player:
        player = self.players.get(player_id)
        if player is None or not player.alive:
            raise GameError("That player is not in the game (or not alive)")
        return player

    def _require_night(self) -> None:
        if self.phase is not Phase.NIGHT:
            raise GameError("There is nothing to do right now")

    def killer_pick(self, player_id: str, target_id: str) -> str:
        """Record a killer's current target. Killers pick simultaneously and
        see each other's choice; the kill *locks* only once every living
        killer has picked the same target. Returns ``"locked"`` on agreement
        (the kill is now final and both killers count as submitted), else
        ``"waiting"`` while the team hasn't converged yet.
        """
        self._require_night()
        actor = self._living(player_id)
        if actor.character is not Character.KILLER:
            raise GameError("You are not a killer")
        if self.kill_locked:
            raise GameError("The kill is already locked in")
        target = self._living(target_id)
        if target.character is Character.KILLER:
            raise GameError("You cannot target a fellow killer")
        self.night_kill_picks[actor.player_id] = target.player_id
        living_killers = self.alive_with(Character.KILLER)
        picks = [self.night_kill_picks.get(k.player_id) for k in living_killers]
        if all(p is not None for p in picks) and len(set(picks)) == 1:
            self.kill_target = picks[0]
            self.kill_locked = True
            for k in living_killers:
                self.night_ready.add(k.player_id)
            return "locked"
        return "waiting"

    def doctor_act(self, player_id: str, target_id: str) -> None:
        self._require_night()
        actor = self._living(player_id)
        if actor.character is not Character.DOCTOR:
            raise GameError("You are not the doctor")
        self.save_target = self._living(target_id).player_id
        self.night_ready.add(actor.player_id)

    def detective_act(self, player_id: str, target_id: str) -> bool:
        self._require_night()
        actor = self._living(player_id)
        if actor.character is not Character.DETECTIVE:
            raise GameError("You are not the detective")
        if target_id == player_id:
            raise GameError("You already know about yourself")
        target = self._living(target_id)
        self.checks.setdefault(actor.player_id, []).append(target.player_id)
        self.night_ready.add(actor.player_id)
        return target.character is Character.KILLER

    def ready_up(self, player_id: str) -> None:
        """A player with no night action (a citizen) taps 'Next' to submit."""
        self._require_night()
        actor = self._living(player_id)
        if actor.character is not Character.CITIZEN:
            raise GameError("You have a night action to take first")
        self.night_ready.add(actor.player_id)

    def night_complete(self) -> bool:
        """True once every living player has submitted their night action."""
        return all(p.player_id in self.night_ready for p in self.alive_players())

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
        """Drop night picks and votes made by / pointing at a removed player."""
        self.night_kill_picks = {
            k: t
            for k, t in self.night_kill_picks.items()
            if k != player_id and t != player_id
        }
        if self.kill_target == player_id:
            self.kill_target = None
            self.kill_locked = False
        if self.save_target == player_id:
            self.save_target = None
        self.night_ready.discard(player_id)
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
    """Roles stay secret through play — a death reveals *that* a player died,
    not *what* they were. Every role is revealed only once the game is over."""
    if game.winner:
        return str(player.character)
    return None


def public_state(game: MafiaGame) -> dict[str, Any]:
    in_day = game.phase is Phase.DAY
    in_night = game.phase is Phase.NIGHT
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
        # Live night progress so every screen can show "3 of 5 ready".
        "alive_count": len(game.alive_players()),
        "night_ready_count": len(game.night_ready) if in_night else 0,
        "winner": game.winner,
    }


def _awaiting_for(game: MafiaGame, player: Player) -> str | None:
    """What action this player still owes for the current night (or None if
    they've submitted / have nothing to do). Everyone acts at once."""
    if not player.alive or game.phase is not Phase.NIGHT:
        return None
    if player.player_id in game.night_ready:
        return None  # already submitted — waiting on the rest of the table
    if player.character is Character.KILLER:
        return "kill"
    if player.character is Character.DOCTOR:
        return "save"
    if player.character is Character.DETECTIVE:
        return "check"
    return "ready"  # citizen — just taps Next


def state_for(game: MafiaGame, player_id: str) -> dict[str, Any]:
    state = public_state(game)
    player = game.players[player_id]
    assigned = game.phase is not Phase.LOBBY
    partner_name: str | None = None
    your_night_pick: str | None = None
    partner_pick: dict[str, str] | None = None
    if assigned and player.character is Character.KILLER:
        partners = [k for k in game.killers() if k.player_id != player.player_id and k.alive]
        partner_name = partners[0].name if partners else None
        # The killer's own submitted pick (so the UI can highlight it) plus
        # the partner's live pick — this is how the duo "sees each other" and
        # converges on a shared target before it locks.
        your_night_pick = game.night_kill_picks.get(player.player_id)
        if partners:
            pp = game.night_kill_picks.get(partners[0].player_id)
            if pp is not None and pp in game.players:
                partner_pick = {"target_id": pp, "target_name": game.players[pp].name}
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
        "submitted": player.player_id in game.night_ready,
        "your_night_pick": your_night_pick,
        "partner_pick": partner_pick,
        "kill_locked": game.kill_locked,
        "check_results": check_results,
        "ghost": ghost,
    }
    return state
