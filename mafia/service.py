from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.service import EnablementDep, Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolDefinition, ToolParameterType

from .game import (
    THEME_PRESETS,
    THEME_SURPRISE,
    Character,
    GameError,
    MafiaGame,
    Phase,
    Player,
    state_for,
)
from .narrator import Narrator

logger = logging.getLogger(__name__)

_DEFAULT_NARRATOR_PROMPT = (
    "You are Gilbert, the narrator of an in-person party game of Mafia. "
    "You tell one continuous, atmospheric story set in the theme you are given, "
    "ominous but playful, suitable for a living room of friends. "
    "Stay strictly consistent with the theme and with every prior story beat: "
    "characters who died stay dead, places and details stay the same. "
    "Narrate only the facts you are given — never invent deaths, accusations, "
    "or clues about who the killers are, and never reveal hidden information."
)


class MafiaService(Service):
    """Runs Mafia games: lobby, night actions, voting, narration."""

    slash_namespace = "mafia"
    config_namespace = "mafia"
    config_category = "Games"

    def __init__(self) -> None:
        self._enabled = False
        self._resolver: ServiceResolver | None = None
        self._config: dict[str, Any] = {}
        self._narrator_prompt = _DEFAULT_NARRATOR_PROMPT
        self._ai_profile = "standard"
        self._speaker_names: list[str] | None = None
        self._volume = 70
        self._nudge_seconds = 45
        self._max_games = 2
        self._games: dict[str, MafiaGame] = {}
        # game_id → player_id → live connections (per-player secret channel)
        self._conns: dict[str, dict[str, set[Any]]] = {}
        self._nudge_tasks: dict[str, asyncio.Task[None]] = {}
        self._beat_tasks: dict[str, asyncio.Task[None]] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="mafia",
            capabilities=frozenset({"mafia_game", "ws_handlers", "ai_tools"}),
            optional=frozenset(
                {"ai_chat", "speaker_control", "event_bus", "configuration", "access_control"}
            ),
            requires_enabled=(EnablementDep(capability="text_to_speech"),),
            toggleable=True,
            toggle_description="In-person Mafia party game narrated aloud by Gilbert.",
        )

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Enable the Mafia party game",
                default=False,
            ),
            ConfigParam(
                key="narrator_prompt",
                type=ToolParameterType.STRING,
                description="System prompt for the Mafia narrator persona",
                default=_DEFAULT_NARRATOR_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description="AI profile used for narration",
                default="standard",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="speakers",
                type=ToolParameterType.ARRAY,
                description="Speakers for narration (empty = default announce speakers)",
                default=None,
                choices_from="speakers",
            ),
            ConfigParam(
                key="announce_volume",
                type=ToolParameterType.INTEGER,
                description="Narration volume (0-100)",
                default=70,
            ),
            ConfigParam(
                key="nudge_seconds",
                type=ToolParameterType.INTEGER,
                description="Seconds of silence before the narrator nudges a stalled phase",
                default=45,
            ),
            ConfigParam(
                key="max_concurrent_games",
                type=ToolParameterType.INTEGER,
                description="Maximum simultaneous Mafia games",
                default=2,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._config.update(config)
        self._narrator_prompt = str(
            self._config.get("narrator_prompt") or _DEFAULT_NARRATOR_PROMPT
        )
        self._ai_profile = str(self._config.get("ai_profile") or "standard")
        raw_speakers = self._config.get("speakers")
        self._speaker_names = [str(s) for s in raw_speakers] if raw_speakers else None
        self._volume = int(self._config.get("announce_volume") or 70)
        self._nudge_seconds = int(self._config.get("nudge_seconds") or 45)
        self._max_games = int(self._config.get("max_concurrent_games") or 2)

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        config_svc = resolver.get_capability("configuration")
        if isinstance(config_svc, ConfigurationReader):
            self._config = dict(config_svc.get_section("mafia"))
        self._enabled = bool(self._config.get("enabled", False))
        await self.on_config_changed(self._config)
        if not self._enabled:
            logger.info("Mafia service registered but disabled")
            return
        logger.info("Mafia service started")

    async def stop(self) -> None:
        self._enabled = False
        for task in (*self._nudge_tasks.values(), *self._beat_tasks.values()):
            task.cancel()
        self._nudge_tasks.clear()
        self._beat_tasks.clear()
        self._games.clear()
        self._conns.clear()

    # --- WS wiring ---

    def get_ws_handlers(self) -> dict[str, Any]:
        if not self._enabled:
            return {}
        return {
            "mafia.game.create": self._ws_game_create,
            "mafia.game.join": self._ws_game_join,
            "mafia.game.resume": self._ws_game_resume,
            "mafia.games.active": self._ws_games_active,
            "mafia.game.start": self._ws_game_start,
            "mafia.night.act": self._ws_night_act,
            "mafia.vote.cast": self._ws_vote_cast,
            "mafia.host.skip_phase": self._ws_host_skip,
            "mafia.host.end_day": self._ws_host_end_day,
            "mafia.host.remove_player": self._ws_host_remove,
            "mafia.host.abort": self._ws_host_abort,
        }

    def get_ws_rpc_roles(self) -> dict[str, str]:
        """Players are ephemeral guests (ADR plugins-0011); handlers do
        per-frame auth via game-scoped player tokens / host account."""
        return {"mafia.": "everyone"}

    @staticmethod
    def _err(frame: dict[str, Any], message: str, code: int = 400) -> dict[str, Any]:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": message, "code": code}

    def _register_conn(self, game_id: str, player_id: str, conn: Any) -> None:
        conns = self._conns.setdefault(game_id, {}).setdefault(player_id, set())
        if conn in conns:
            return
        conns.add(conn)

        def _cleanup() -> None:
            game_conns = self._conns.get(game_id, {})
            game_conns.get(player_id, set()).discard(conn)

        conn.add_close_callback(_cleanup)

    def _push_state(self, game: MafiaGame) -> None:
        """Per-player filtered state to every live connection of the game."""
        for player_id, conns in self._conns.get(game.game_id, {}).items():
            if player_id not in game.players:
                continue
            state = state_for(game, player_id)
            frame = {"type": "mafia.state", "game_id": game.game_id, "state": state}
            for conn in list(conns):
                conn.enqueue(frame)

    def _game_and_player(self, frame: dict[str, Any]) -> tuple[MafiaGame, Any] | dict[str, Any]:
        game = self._games.get(str(frame.get("game_id", "")))
        if game is None:
            return self._err(frame, "Game not found", 404)
        player = game.player_by_token(str(frame.get("player_token", "")))
        if player is None:
            return self._err(frame, "Not a player in this game", 403)
        return game, player

    def _require_host(self, conn: Any, frame: dict[str, Any]) -> MafiaGame | dict[str, Any]:
        game = self._games.get(str(frame.get("game_id", "")))
        if game is None:
            return self._err(frame, "Game not found", 404)
        if conn.user_ctx.user_id != game.host_user_id:
            return self._err(frame, "Only the host can do that", 403)
        return game

    # --- lobby handlers ---

    async def _ws_game_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        user_id = conn.user_ctx.user_id
        if conn.user_level > 100 or user_id in ("", "guest"):
            return self._err(frame, "Creating a game needs a signed-in account", 403)
        active = [g for g in self._games.values() if g.phase is not Phase.ENDED]
        if len(active) >= self._max_games:
            return self._err(frame, "Too many games running — finish one first")
        theme_key = str(frame.get("theme_key", "") or THEME_SURPRISE)
        theme_text = str(frame.get("theme_text", "")).strip()
        theme = ""
        if theme_key == "custom":
            if not theme_text:
                return self._err(frame, "Describe your custom theme")
            theme = theme_text
        elif theme_key != THEME_SURPRISE:
            preset = dict(THEME_PRESETS).get(theme_key)
            if preset is None:
                return self._err(frame, f"Unknown theme {theme_key!r}")
            theme = preset
        game = MafiaGame(
            host_user_id=user_id,
            host_name=conn.user_ctx.display_name,
            theme=theme,
            theme_key=theme_key,
        )
        host_player = game.add_player(conn.user_ctx.display_name, user_id=user_id)
        self._games[game.game_id] = game
        self._register_conn(game.game_id, host_player.player_id, conn)
        return {
            "type": "mafia.game.create.result",
            "ref": frame.get("id"),
            "game_id": game.game_id,
            "join_code": game.join_code,
            "player_id": host_player.player_id,
            "player_token": host_player.token,
            "state": state_for(game, host_player.player_id),
        }

    async def _ws_game_join(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        code = str(frame.get("join_code", "")).strip().upper()
        game = next((g for g in self._games.values() if g.join_code == code), None)
        if game is None:
            return self._err(frame, "No game with that code", 404)
        try:
            player = game.add_player(
                str(frame.get("name", "")),
                user_id=conn.user_ctx.user_id if conn.user_ctx.user_id != "guest" else "",
            )
        except GameError as exc:
            return self._err(frame, str(exc))
        self._register_conn(game.game_id, player.player_id, conn)
        self._push_state(game)
        return {
            "type": "mafia.game.join.result",
            "ref": frame.get("id"),
            "game_id": game.game_id,
            "player_id": player.player_id,
            "player_token": player.token,
            "state": state_for(game, player.player_id),
        }

    async def _ws_game_resume(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._game_and_player(frame)
        if isinstance(result, dict):
            return result
        game, player = result
        self._register_conn(game.game_id, player.player_id, conn)
        return {
            "type": "mafia.game.resume.result",
            "ref": frame.get("id"),
            "state": state_for(game, player.player_id),
        }

    async def _ws_games_active(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "mafia.games.active.result",
            "ref": frame.get("id"),
            "games": [
                {
                    "game_id": g.game_id,
                    "join_code": g.join_code if g.phase is Phase.LOBBY else "",
                    "host_name": g.host_name,
                    "phase": str(g.phase),
                    "player_count": len(g.players),
                }
                for g in self._games.values()
                if g.phase is not Phase.ENDED
            ],
        }

    # --- game loop ---

    async def _ws_game_start(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        if game.phase is not Phase.LOBBY:
            return self._err(frame, "The game has already started")
        narrator = self._narrator()
        if game.theme_key == THEME_SURPRISE and not game.theme:
            game.theme = await narrator.invent_theme()
        try:
            game.assign_characters()
        except GameError as exc:
            return self._err(frame, str(exc))
        names = ", ".join(p.name for p in game.players.values())
        await narrator.cue(game, "intro", f"The inhabitants: {names}.")
        game.begin_night()
        await narrator.cue(
            game, "night", "Night falls. Killers, open your eyes and choose a victim."
        )
        self._push_state(game)
        self._restart_nudge(game)
        return {"type": "mafia.game.start.result", "ref": frame.get("id")}

    async def _ws_night_act(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._game_and_player(frame)
        if isinstance(result, dict):
            return result
        game, player = result
        target_id = str(frame.get("target_id", ""))
        extra: dict[str, Any] = {}
        try:
            if game.phase is Phase.NIGHT_KILLERS:
                outcome = game.killer_act(player.player_id, target_id)
                if outcome == "confirmed":
                    await self._advance_night(game)
            elif game.phase is Phase.NIGHT_DOCTOR:
                game.doctor_act(player.player_id, target_id)
                await self._advance_night(game)
            elif game.phase is Phase.NIGHT_DETECTIVE:
                extra["is_killer"] = game.detective_act(player.player_id, target_id)
                await self._advance_night(game)
            else:
                return self._err(frame, "There is nothing to do right now")
        except GameError as exc:
            return self._err(frame, str(exc))
        self._push_state(game)
        self._restart_nudge(game)
        return {"type": "mafia.night.act.result", "ref": frame.get("id"), **extra}

    async def _advance_night(self, game: MafiaGame) -> None:
        """Move to the next living special, or resolve the night at dawn."""
        narrator = self._narrator()
        if game.phase is Phase.NIGHT_KILLERS and game.alive_with(Character.DOCTOR):
            game.phase = Phase.NIGHT_DOCTOR
            await narrator.speak("Killers, close your eyes. Doctor, open yours — who will you save?")
            return
        if game.phase in (Phase.NIGHT_KILLERS, Phase.NIGHT_DOCTOR) and game.alive_with(
            Character.DETECTIVE
        ):
            game.phase = Phase.NIGHT_DETECTIVE
            await narrator.speak(
                "Close your eyes. Detective, open yours — whose secret will you learn?"
            )
            return
        await self._dawn(game)

    async def _dawn(self, game: MafiaGame) -> None:
        game.phase = Phase.DAWN
        narrator = self._narrator()
        victim = game.resolve_night()
        if victim is None:
            facts = "Nobody died last night — the town wakes relieved."
        else:
            facts = (
                f"{victim.name} was killed in the night. "
                f"They were the {victim.character}. Reveal this inside the story."
            )
        await narrator.cue(game, "dawn", facts)
        if await self._maybe_finish(game):
            return
        game.phase = Phase.DAY
        await narrator.speak("Everyone, open your eyes. Discuss — then vote on your phones.")

    async def _ws_vote_cast(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._game_and_player(frame)
        if isinstance(result, dict):
            return result
        game, player = result
        raw = frame.get("target")
        target = None if raw is None else str(raw)
        try:
            game.cast_vote(player.player_id, target)
        except GameError as exc:
            return self._err(frame, str(exc))
        chosen = game.majority_target()
        if chosen is not None:
            await self._dusk(game, eliminated=chosen)
        self._push_state(game)
        return {"type": "mafia.vote.cast.result", "ref": frame.get("id")}

    async def _dusk(self, game: MafiaGame, eliminated: Player | None) -> None:
        game.phase = Phase.DUSK
        narrator = self._narrator()
        await narrator.speak("The town has decided. Everyone, close your eyes.")
        if eliminated is not None:
            game.eliminate(eliminated.player_id)
            facts = (
                f"The town cast out {eliminated.name}. They were the "
                f"{eliminated.character}. Reveal this inside the story."
            )
        else:
            facts = "The town argued until sundown but could not agree. Nobody was cast out."
        await narrator.cue(game, "dusk", facts)
        if await self._maybe_finish(game):
            return
        game.begin_night()
        await narrator.speak("Night falls again. Killers, open your eyes.")
        self._restart_nudge(game)

    async def _maybe_finish(self, game: MafiaGame) -> bool:
        winner = game.check_winner()
        if not winner:
            return False
        game.winner = winner
        game.phase = Phase.ENDED
        narrator = self._narrator()
        killers = ", ".join(k.name for k in game.killers())
        if winner == "citizens":
            facts = f"The killers ({killers}) are all gone. The town survives."
        else:
            facts = f"The killers ({killers}) now hold the town. Darkness wins."
        await narrator.cue(game, "win", facts)
        self._cancel_nudge(game.game_id)
        self._push_state(game)
        return True

    # --- host powers ---

    async def _ws_host_skip(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        if game.phase not in (Phase.NIGHT_KILLERS, Phase.NIGHT_DOCTOR, Phase.NIGHT_DETECTIVE):
            return self._err(frame, "Nothing to skip right now")
        await self._advance_night(game)
        self._push_state(game)
        self._restart_nudge(game)
        return {"type": "mafia.host.skip_phase.result", "ref": frame.get("id")}

    async def _ws_host_end_day(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        if game.phase is not Phase.DAY:
            return self._err(frame, "It is not daytime")
        await self._dusk(game, eliminated=None)
        self._push_state(game)
        return {"type": "mafia.host.end_day.result", "ref": frame.get("id")}

    async def _ws_host_remove(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        player_id = str(frame.get("player_id", ""))
        player = game.players.get(player_id)
        if player is None or not player.alive:
            return self._err(frame, "No such living player", 404)
        game.eliminate(player_id)
        game.purge_references(player_id)
        narrator = self._narrator()
        await narrator.cue(
            game,
            "dusk",
            f"{player.name} has left the story. They were the {player.character}.",
        )
        if not await self._maybe_finish(game):
            # Advance only when the removed player was the awaited night actor —
            # removing a bystander must not forfeit the pending kill/save/check.
            phase_char = {
                Phase.NIGHT_KILLERS: Character.KILLER,
                Phase.NIGHT_DOCTOR: Character.DOCTOR,
                Phase.NIGHT_DETECTIVE: Character.DETECTIVE,
            }.get(game.phase)
            if phase_char is not None and not game.alive_with(phase_char):
                await self._advance_night(game)
        self._push_state(game)
        return {"type": "mafia.host.remove_player.result", "ref": frame.get("id")}

    async def _ws_host_abort(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        game.winner = "aborted"
        game.phase = Phase.ENDED
        self._cancel_nudge(game.game_id)
        self._push_state(game)
        self._games.pop(game.game_id, None)
        self._conns.pop(game.game_id, None)
        return {"type": "mafia.host.abort.result", "ref": frame.get("id")}

    # --- nudges ---

    def _cancel_nudge(self, game_id: str) -> None:
        task = self._nudge_tasks.pop(game_id, None)
        if task is not None:
            task.cancel()

    def _restart_nudge(self, game: MafiaGame) -> None:
        self._cancel_nudge(game.game_id)
        if game.phase not in (Phase.NIGHT_KILLERS, Phase.NIGHT_DOCTOR, Phase.NIGHT_DETECTIVE):
            return
        self._nudge_tasks[game.game_id] = asyncio.get_running_loop().create_task(
            self._nudge_loop(game.game_id), context=contextvars.copy_context()
        )

    async def _nudge_loop(self, game_id: str) -> None:
        while True:
            await asyncio.sleep(self._nudge_seconds)
            game = self._games.get(game_id)
            if game is None or game.phase not in (
                Phase.NIGHT_KILLERS,
                Phase.NIGHT_DOCTOR,
                Phase.NIGHT_DETECTIVE,
            ):
                return
            try:
                await self._narrator().cue(
                    game, "nudge", "Someone in the dark is taking their time."
                )
            except Exception:
                logger.exception("Mafia nudge failed")

    def _narrator(self) -> Narrator:
        """Build a :class:`Narrator` wired to the current config and resolved capabilities."""
        assert self._resolver is not None
        return Narrator(
            ai=self._resolver.get_capability("ai_chat"),
            speaker=self._resolver.get_capability("speaker_control"),
            system_prompt=self._narrator_prompt,
            ai_profile=self._ai_profile,
            speaker_names=self._speaker_names,
            volume=self._volume,
        )

    # --- ToolProvider ---

    @property
    def tool_provider_name(self) -> str:
        return "mafia"

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="mafia_open",
                description=(
                    "Point users at the Mafia party game page. Call when someone "
                    "wants to play Mafia (werewolf-style social deduction). The game "
                    "itself is played at /mafia on each player's phone — this tool "
                    "only returns the link and any open lobby join codes."
                ),
                parameters=[],
                required_role="everyone",
                slash_command="open",
                slash_help="Open the Mafia party game",
                parallel_safe=True,
            )
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "mafia_open":
            raise KeyError(name)
        lines = ["Gather everyone and open **[Open Mafia](/mafia)** on each phone."]
        lobbies = [g for g in self._games.values() if g.phase is Phase.LOBBY]
        for g in lobbies:
            lines.append(f"- {g.host_name}'s game is open — join code **{g.join_code}**")
        return "\n".join(lines)
