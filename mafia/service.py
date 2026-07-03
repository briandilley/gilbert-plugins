from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.service import EnablementDep, Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import SpeakerLister
from gilbert.interfaces.tools import ToolDefinition, ToolParameterType
from gilbert.interfaces.ws import WsConnectionBase

from .game import (
    THEME_PRESETS,
    THEME_SURPRISE,
    GameError,
    MafiaGame,
    Phase,
    Player,
    state_for,
)
from .narrator import NarrationPrompts, Narrator

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

# Per-beat narrator instructions. Each is a user-editable ``ai_prompt``
# ConfigParam; these are the bundled defaults. Keyed by beat name to match
# Narrator.narrate(beat=...) / nudge().
_DEFAULT_BEAT_INTRO = (
    "Open the game: set the scene and name the players as inhabitants. Tell "
    "them a killer hides among them and that each night everyone secretly "
    "makes their move at the same time."
)
_DEFAULT_BEAT_NIGHT = (
    "Briefly narrate night falling over the setting as everyone secretly "
    "makes their choice."
)
_DEFAULT_BEAT_DAWN = (
    "Narrate the morning as described in the facts — reveal who was found "
    "dead, or that everyone survived. Do NOT reveal the victim's role. Then "
    "turn the town to discussion and the day's vote."
)
_DEFAULT_BEAT_DUSK = (
    "Narrate the outcome of the town's vote as described in the facts. Do NOT "
    "reveal the cast-out player's role. Then night returns."
)
_DEFAULT_BEAT_NUDGE = (
    "One short in-character line gently hurrying whoever still hasn't made "
    "their choice tonight. Do not name anyone."
)
_DEFAULT_BEAT_WIN = (
    "Narrate the finale described in the facts, then congratulate the winners "
    "and reveal nothing else."
)
# Style/length guidance appended after the beat instruction.
_DEFAULT_NARRATE_STYLE = (
    "Write 2-4 sentences. Spoken aloud, so no stage directions or markdown."
)
_DEFAULT_NUDGE_STYLE = (
    "Write one short sentence. Spoken aloud, so no stage directions or markdown."
)
# Prompt used to invent a "Surprise me" theme.
_DEFAULT_INVENT_THEME = (
    "Invent an evocative setting for a murder-mystery party game. Reply with a "
    "single short sentence describing the setting and nothing else."
)

# Narration volume used when the host doesn't set one for a game (0-100).
_DEFAULT_VOLUME = 70


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
        self._beats: dict[str, str] = {
            "intro": _DEFAULT_BEAT_INTRO,
            "night": _DEFAULT_BEAT_NIGHT,
            "dawn": _DEFAULT_BEAT_DAWN,
            "dusk": _DEFAULT_BEAT_DUSK,
            "nudge": _DEFAULT_BEAT_NUDGE,
            "win": _DEFAULT_BEAT_WIN,
        }
        self._narrate_style = _DEFAULT_NARRATE_STYLE
        self._nudge_style = _DEFAULT_NUDGE_STYLE
        self._invent_theme_prompt = _DEFAULT_INVENT_THEME
        self._ai_profile = "standard"
        self._nudge_seconds = 45
        self._max_games = 2
        self._games: dict[str, MafiaGame] = {}
        # game_id → player_id → live connections (per-player secret channel)
        self._conns: dict[str, dict[str, set[Any]]] = {}
        self._nudge_tasks: dict[str, asyncio.Task[None]] = {}

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
                key="beat_intro_prompt",
                type=ToolParameterType.STRING,
                description="Narrator instruction for the opening beat (game start)",
                default=_DEFAULT_BEAT_INTRO,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="beat_night_prompt",
                type=ToolParameterType.STRING,
                description="Narrator instruction for nightfall beats",
                default=_DEFAULT_BEAT_NIGHT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="beat_dawn_prompt",
                type=ToolParameterType.STRING,
                description="Narrator instruction for the dawn / morning-discovery beat",
                default=_DEFAULT_BEAT_DAWN,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="beat_dusk_prompt",
                type=ToolParameterType.STRING,
                description="Narrator instruction for the dusk / post-vote beat",
                default=_DEFAULT_BEAT_DUSK,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="beat_nudge_prompt",
                type=ToolParameterType.STRING,
                description="Narrator instruction for stall nudges (a hesitating phase)",
                default=_DEFAULT_BEAT_NUDGE,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="beat_win_prompt",
                type=ToolParameterType.STRING,
                description="Narrator instruction for the finale / winner beat",
                default=_DEFAULT_BEAT_WIN,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="narrate_style_prompt",
                type=ToolParameterType.STRING,
                description="Style/length guidance appended to every story beat",
                default=_DEFAULT_NARRATE_STYLE,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="nudge_style_prompt",
                type=ToolParameterType.STRING,
                description="Style/length guidance appended to stall nudges",
                default=_DEFAULT_NUDGE_STYLE,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="invent_theme_prompt",
                type=ToolParameterType.STRING,
                description="Prompt used to invent a 'Surprise me' theme",
                default=_DEFAULT_INVENT_THEME,
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
        self._beats = {
            "intro": str(self._config.get("beat_intro_prompt") or _DEFAULT_BEAT_INTRO),
            "night": str(self._config.get("beat_night_prompt") or _DEFAULT_BEAT_NIGHT),
            "dawn": str(self._config.get("beat_dawn_prompt") or _DEFAULT_BEAT_DAWN),
            "dusk": str(self._config.get("beat_dusk_prompt") or _DEFAULT_BEAT_DUSK),
            "nudge": str(self._config.get("beat_nudge_prompt") or _DEFAULT_BEAT_NUDGE),
            "win": str(self._config.get("beat_win_prompt") or _DEFAULT_BEAT_WIN),
        }
        self._narrate_style = str(
            self._config.get("narrate_style_prompt") or _DEFAULT_NARRATE_STYLE
        )
        self._nudge_style = str(
            self._config.get("nudge_style_prompt") or _DEFAULT_NUDGE_STYLE
        )
        self._invent_theme_prompt = str(
            self._config.get("invent_theme_prompt") or _DEFAULT_INVENT_THEME
        )
        self._ai_profile = str(self._config.get("ai_profile") or "standard")
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
        for task in self._nudge_tasks.values():
            task.cancel()
        self._nudge_tasks.clear()
        self._games.clear()
        self._conns.clear()

    # --- WS wiring ---

    def get_ws_handlers(self) -> dict[str, Any]:
        # Discovered ONCE at startup (WsConnectionManager.subscribe_to_bus
        # runs exactly once) -- gating this on self._enabled would mean a
        # service left disabled at boot never gets its RPC surface wired
        # up, even after being toggled on later, until a full process
        # restart. Always register the frame types; each handler checks
        # self._enabled itself via _disabled_err() so toggling off closes
        # the surface immediately without waiting for a restart.
        return {
            "mafia.speakers.list": self._ws_speakers_list,
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

    def _disabled_err(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Return a 403 error frame if the service is toggled off, else None.

        get_ws_handlers() registers mafia.* frame types unconditionally
        (see its docstring) since discovery only happens once at startup;
        this per-call guard is what actually closes the RPC surface when
        the service is disabled at runtime.
        """
        if not self._enabled:
            return self._err(frame, "The Mafia game is disabled", 403)
        return None

    def _register_conn(self, game_id: str, player_id: str, conn: WsConnectionBase) -> None:
        conns = self._conns.setdefault(game_id, {}).setdefault(player_id, set())
        if conn in conns:
            return
        conns.add(conn)

        def _cleanup() -> None:
            game_conns = self._conns.get(game_id, {})
            game_conns.get(player_id, set()).discard(conn)

        conn.add_close_callback(_cleanup)

    def _push_state(self, game: MafiaGame, status: str = "") -> None:
        """Per-player filtered state to every live connection of the game.

        ``status`` is a transient "what's happening now" line (e.g.
        "Calculating the night…"). Pushed just before a slow narration so
        screens show that Gilbert is working rather than looking frozen; the
        next plain push (no status) clears it.
        """
        for player_id, conns in self._conns.get(game.game_id, {}).items():
            if player_id not in game.players:
                continue
            state = state_for(game, player_id)
            if status:
                state["status"] = status
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

    def _require_host(self, conn: WsConnectionBase, frame: dict[str, Any]) -> MafiaGame | dict[str, Any]:
        game = self._games.get(str(frame.get("game_id", "")))
        if game is None:
            return self._err(frame, "Game not found", 404)
        if conn.user_ctx.user_id != game.host_user_id:
            return self._err(frame, "Only the host can do that", 403)
        return game

    # --- lobby handlers ---

    async def _ws_speakers_list(
        self, conn: WsConnectionBase, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """List the speakers the host can pick from when creating a game.

        Speaker + volume are a *per-game* choice (see ``MafiaGame``), set
        in the create form — this feeds that picker. A fresh, on-demand
        enumeration (not the settings cache) so a just-activated browser
        speaker or a late Sonos discovery shows up. Narration is spoken by
        game events from background tasks with no caller context, so we
        return each speaker under its stable display name (no caller-scoped
        "my browser" alias, which couldn't resolve outside a request).
        """
        if (err := self._disabled_err(frame)) is not None:
            return err
        speakers: list[dict[str, Any]] = []
        assert self._resolver is not None
        speaker_svc = self._resolver.get_capability("speaker_control")
        if isinstance(speaker_svc, SpeakerLister):
            try:
                live = await speaker_svc.list_speakers()
            except Exception:
                logger.exception("Mafia speakers.list live fetch failed")
                live = []
            speakers = [
                {
                    "id": s.name,
                    "name": s.name,
                    "model": getattr(s, "model", "") or "",
                    "backend": getattr(s, "backend_name", "") or "",
                    "group_name": getattr(s, "group_name", "") or "",
                }
                for s in live
            ]
        return {
            "type": "mafia.speakers.list.result",
            "ref": frame.get("id"),
            "speakers": speakers,
            "defaults": {"volume": _DEFAULT_VOLUME},
        }

    @staticmethod
    def _parse_narration(frame: dict[str, Any]) -> tuple[list[str] | None, int]:
        """Pull the host's per-game speaker names + volume out of a create frame.

        No speakers selected → ``None`` (fall back to the default announce
        speakers). Volume is clamped to 0-100.
        """
        raw_speakers = frame.get("speaker_names")
        speaker_names = (
            [str(s) for s in raw_speakers]
            if isinstance(raw_speakers, list) and raw_speakers
            else None
        )
        try:
            volume = int(frame.get("volume", _DEFAULT_VOLUME))
        except (TypeError, ValueError):
            volume = _DEFAULT_VOLUME
        volume = max(0, min(100, volume))
        return speaker_names, volume

    async def _ws_game_create(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
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
        host_name = conn.user_ctx.display_name or conn.user_ctx.user_id or "Host"
        speaker_names, volume = self._parse_narration(frame)
        game = MafiaGame(
            host_user_id=user_id,
            host_name=host_name,
            theme=theme,
            theme_key=theme_key,
            speaker_names=speaker_names,
            volume=volume,
        )
        host_player = game.add_player(host_name, user_id=user_id)
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

    async def _ws_game_join(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
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

    async def _ws_game_resume(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
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

    async def _ws_games_active(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
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

    async def _ws_game_start(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        if game.phase is not Phase.LOBBY:
            return self._err(frame, "The game has already started")
        try:
            game.assign_characters()
        except GameError as exc:
            return self._err(frame, str(exc))
        # Everything from the LOBBY check above through this assignment is
        # synchronous (no ``await``), so it can't be interleaved with a
        # concurrent start or join. Claim the game *before* the first await
        # (theme invention / narration, which can take seconds): a second
        # concurrent start then sees a non-LOBBY phase and bails with
        # "already started" instead of reshuffling characters or
        # double-running the night, and add_player's own LOBBY-only check
        # rejects a join that arrives mid-start. If assign_characters()
        # raised above, the phase never left LOBBY.
        game.phase = Phase.NIGHT  # non-LOBBY placeholder meaning "starting"
        # Lift everyone out of the lobby into a "starting" screen while the
        # (slow) theme + intro narration runs.
        self._push_state(game, status="Setting the scene…")
        narrator = self._narrator(game)
        if game.theme_key == THEME_SURPRISE and not game.theme:
            game.theme = await narrator.invent_theme()
        names = ", ".join(p.name for p in game.players.values())
        await narrator.cue(game, "intro", f"The inhabitants: {names}.")
        game.begin_night()
        # Show the night board before the (slow) narration so screens flip to
        # the new round immediately rather than after the AI/TTS round-trip.
        self._push_state(game)
        await narrator.cue(game, "night", "Night falls. Everyone, make your choice.")
        self._push_state(game)
        self._restart_nudge(game)
        return {"type": "mafia.game.start.result", "ref": frame.get("id")}

    async def _ws_night_act(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        """Handle one player's simultaneous night submission.

        Frame carries ``action`` (``kill`` / ``save`` / ``check`` / ``ready``)
        and, for the first three, a ``target_id``. Everyone submits at once;
        once every living player has, the night resolves into the day.
        """
        if (err := self._disabled_err(frame)) is not None:
            return err
        result = self._game_and_player(frame)
        if isinstance(result, dict):
            return result
        game, player = result
        if game.phase is not Phase.NIGHT:
            return self._err(frame, "There is nothing to do right now")
        action = str(frame.get("action", ""))
        target_id = str(frame.get("target_id", ""))
        extra: dict[str, Any] = {}
        try:
            if action == "kill":
                game.killer_pick(player.player_id, target_id)
            elif action == "save":
                game.doctor_act(player.player_id, target_id)
            elif action == "check":
                extra["is_killer"] = game.detective_act(player.player_id, target_id)
            elif action == "ready":
                game.ready_up(player.player_id)
            else:
                return self._err(frame, "Unknown night action")
        except GameError as exc:
            return self._err(frame, str(exc))
        if game.night_complete():
            await self._resolve_night(game)
        else:
            # Live update so killers see each other's picks converge and
            # everyone sees the "N of M ready" counter tick up.
            self._push_state(game)
        self._restart_nudge(game)
        return {"type": "mafia.night.act.result", "ref": frame.get("id"), **extra}

    async def _resolve_night(self, game: MafiaGame) -> None:
        """Resolve the simultaneous night (kill vs. save) and open the day.

        Announces *that* someone died, never their role (revealed only at
        game end). Shows a "calculating" status while the narration runs so
        screens don't look frozen.
        """
        # Everyone is in (or the host forced it) — show that Gilbert is
        # working out the outcome during the slow reveal narration.
        self._push_state(game, status="Calculating the night…")
        narrator = self._narrator(game)
        victim = game.resolve_night()
        if victim is None:
            facts = "Nobody died last night — the town wakes whole."
        else:
            facts = f"{victim.name} did not survive the night."
        await narrator.cue(game, "dawn", facts)
        if await self._maybe_finish(game):
            return
        game.phase = Phase.DAY
        self._push_state(game)

    async def _ws_vote_cast(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
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
            await self._resolve_day(game, eliminated=chosen)
        self._push_state(game)
        return {"type": "mafia.vote.cast.result", "ref": frame.get("id")}

    async def _resolve_day(self, game: MafiaGame, eliminated: Player | None) -> None:
        """Resolve the day vote and return to a fresh simultaneous night.

        Announces *who* the town cast out, never their role (revealed only at
        game end). Shows a "tallying" status during the verdict narration."""
        self._push_state(game, status="Tallying the vote…")
        narrator = self._narrator(game)
        if eliminated is not None:
            game.eliminate(eliminated.player_id)
            facts = f"The town voted to cast out {eliminated.name}."
        else:
            facts = "The town argued until sundown but could not agree. Nobody was cast out."
        await narrator.cue(game, "dusk", facts)
        if await self._maybe_finish(game):
            return
        game.begin_night()
        # Flip screens to the fresh night board before the night narration.
        self._push_state(game)
        await narrator.cue(game, "night", "Night falls again. Everyone, make your choice.")
        self._restart_nudge(game)

    async def _maybe_finish(self, game: MafiaGame) -> bool:
        winner = game.check_winner()
        if not winner:
            return False
        game.winner = winner
        game.phase = Phase.ENDED
        narrator = self._narrator(game)
        killers = ", ".join(k.name for k in game.killers())
        if winner == "citizens":
            facts = f"The killers ({killers}) are all gone. The town survives."
        else:
            facts = f"The killers ({killers}) now hold the town. Darkness wins."
        await narrator.cue(game, "win", facts)
        self._cancel_nudge(game.game_id)
        self._push_state(game)
        # Ended games would otherwise be retained forever — unbounded memory
        # growth, and a join-code collision with a retained ENDED game can
        # block a new lobby from forming. The final state was already
        # pushed above, so every ghost keeps it client-side; drop the
        # server-side copy the same way _ws_host_abort does.
        self._games.pop(game.game_id, None)
        self._conns.pop(game.game_id, None)
        return True

    # --- host powers ---

    async def _ws_host_skip(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        if game.phase is not Phase.NIGHT:
            return self._err(frame, "Nothing to skip right now")
        # Force the night to resolve with whatever is already submitted — an
        # unlocked kill means nobody dies. Escape hatch when a killer duo
        # won't converge or a citizen never taps Next.
        await self._resolve_night(game)
        self._push_state(game)
        self._restart_nudge(game)
        return {"type": "mafia.host.skip_phase.result", "ref": frame.get("id")}

    async def _ws_host_end_day(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        if game.phase is not Phase.DAY:
            return self._err(frame, "It is not daytime")
        await self._resolve_day(game, eliminated=None)
        self._push_state(game)
        return {"type": "mafia.host.end_day.result", "ref": frame.get("id")}

    async def _ws_host_remove(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
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
        narrator = self._narrator(game)
        # Announce the departure without exposing their role.
        await narrator.cue(game, "dusk", f"{player.name} has left the story.")
        if not await self._maybe_finish(game):
            if game.phase is Phase.DAY:
                # Removing a bystander can drop the alive count enough that
                # an existing vote tally now clears the (now-lower) majority
                # threshold — resolve it immediately rather than waiting for
                # a vote that may never come.
                target = game.majority_target()
                if target is not None:
                    await self._resolve_day(game, eliminated=target)
            elif game.phase is Phase.NIGHT and game.night_complete():
                # Removing the last player the table was waiting on (they
                # had no pending action, or their action is now moot) can
                # complete the simultaneous night — resolve it.
                await self._resolve_night(game)
        self._push_state(game)
        return {"type": "mafia.host.remove_player.result", "ref": frame.get("id")}

    async def _ws_host_abort(self, conn: WsConnectionBase, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := self._disabled_err(frame)) is not None:
            return err
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
        if game.phase is not Phase.NIGHT:
            return
        self._nudge_tasks[game.game_id] = asyncio.get_running_loop().create_task(
            self._nudge_loop(game.game_id), context=contextvars.copy_context()
        )

    async def _nudge_loop(self, game_id: str) -> None:
        while True:
            await asyncio.sleep(self._nudge_seconds)
            game = self._games.get(game_id)
            if game is None or game.phase is not Phase.NIGHT:
                return
            try:
                await self._narrator(game).nudge(game)
            except Exception:
                logger.exception("Mafia nudge failed")

    def _narrator(self, game: MafiaGame) -> Narrator:
        """Build a :class:`Narrator` for ``game``.

        Narration prompts come from the service's (user-editable) config;
        the speakers and volume come from ``game`` itself (per-game choice).
        """
        assert self._resolver is not None
        prompts = NarrationPrompts(
            system=self._narrator_prompt,
            beats=dict(self._beats),
            narrate_style=self._narrate_style,
            nudge_style=self._nudge_style,
            invent_theme=self._invent_theme_prompt,
        )
        return Narrator(
            ai=self._resolver.get_capability("ai_chat"),
            speaker=self._resolver.get_capability("speaker_control"),
            prompts=prompts,
            ai_profile=self._ai_profile,
            speaker_names=game.speaker_names,
            volume=game.volume,
        )

    # --- ToolProvider ---

    @property
    def tool_provider_name(self) -> str:
        return "mafia"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
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
