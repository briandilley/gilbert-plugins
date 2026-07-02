from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.service import EnablementDep, Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

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
