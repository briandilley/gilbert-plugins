from __future__ import annotations

import asyncio
import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.service import EnablementDep, Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

from .game import (
    THEME_PRESETS,
    THEME_SURPRISE,
    GameError,
    MafiaGame,
    Phase,
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

    # --- Task 10 stubs (kept truthful with get_ws_handlers; replaced there) ---

    async def _ws_game_start(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return self._err(frame, "Not implemented yet", 400)

    async def _ws_night_act(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return self._err(frame, "Not implemented yet", 400)

    async def _ws_vote_cast(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return self._err(frame, "Not implemented yet", 400)

    async def _ws_host_skip(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return self._err(frame, "Not implemented yet", 400)

    async def _ws_host_end_day(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return self._err(frame, "Not implemented yet", 400)

    async def _ws_host_remove(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return self._err(frame, "Not implemented yet", 400)

    async def _ws_host_abort(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return self._err(frame, "Not implemented yet", 400)

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
