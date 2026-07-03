"""Narration engine: themed, story-consistent beats via one-shot AI calls.

Killer identities are never given to the model — only public facts
(deaths, revealed characters) plus the running story. AI calls use
complete_one_shot(tools_override=[]) per core ADR-0010; chat() would
persist a conversation per beat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole
from gilbert.interfaces.speaker import SpeakerProvider

from .game import MafiaGame

logger = logging.getLogger(__name__)

_MAX_STORY_LINES = 20  # cap prompt growth on long games


@dataclass(frozen=True)
class NarrationPrompts:
    """The tunable prompt strings that shape narration.

    All of these are user-editable ``ai_prompt`` ConfigParams on
    ``MafiaService``; the service resolves the active values (falling
    back to its bundled defaults) and hands a fully-populated instance
    to each :class:`Narrator` it builds. The Narrator never reads
    defaults itself — it narrates with whatever it's given.

    - ``system`` — the narrator persona (``system_prompt`` on every call).
    - ``beats`` — per-beat instruction keyed by beat name
      (``intro`` / ``night`` / ``dawn`` / ``dusk`` / ``nudge`` / ``win``).
    - ``narrate_style`` — style/length guidance appended to every story beat.
    - ``nudge_style`` — style/length guidance appended to stall nudges.
    - ``invent_theme`` — the prompt used to invent a "Surprise me" theme.
    """

    system: str
    beats: dict[str, str]
    narrate_style: str
    nudge_style: str
    invent_theme: str


class Narrator:
    """Builds prompts, calls the AI, speaks the result. All I/O degrades gracefully."""

    def __init__(
        self,
        *,
        ai: Any,
        speaker: Any,
        prompts: NarrationPrompts,
        ai_profile: str,
        speaker_names: list[str] | None,
        volume: int | None,
    ) -> None:
        self._ai = ai
        self._speaker = speaker
        self._prompts = prompts
        self._ai_profile = ai_profile
        self._speaker_names = speaker_names
        self._volume = volume

    async def invent_theme(self) -> str:
        """One-shot ask the AI for a 1-sentence murder-mystery setting."""
        text = await self._one_shot(self._prompts.invent_theme)
        return text or "A small town where everyone knows everyone"

    async def narrate(self, game: MafiaGame, beat: str, facts: str) -> str:
        """Narrate ``facts`` as the next story beat, appending it to ``game.story``.

        Falls back to returning ``facts`` verbatim when the AI is absent or errors.
        """
        story_tail = game.story[-_MAX_STORY_LINES:]
        parts = [
            f"Theme / setting (stay strictly consistent with it): {game.theme}",
            "Story so far:" if story_tail else "This is the very first beat of the story.",
            *story_tail,
            f"Facts to narrate now (do not contradict or invent deaths): {facts}",
            self._prompts.beats.get(beat, ""),
            self._prompts.narrate_style,
        ]
        text = await self._one_shot("\n".join(p for p in parts if p))
        if not text:
            text = facts
        game.story.append(text)
        return text

    async def speak(
        self, text: str, *, context: str = "ominous but playful murder-mystery narrator"
    ) -> None:
        """Announce ``text`` over the configured speakers, swallowing all errors."""
        if not isinstance(self._speaker, SpeakerProvider):
            logger.debug("No speaker service — narration not spoken: %s", text)
            return
        try:
            await self._speaker.announce(
                text,
                speaker_names=self._speaker_names,
                volume=self._volume,
                context=context,
            )
        except Exception:
            logger.exception("Mafia narration announce failed")

    async def cue(self, game: MafiaGame, beat: str, facts: str) -> str:
        """Narrate ``facts`` and speak the result, returning the narrated text."""
        text = await self.narrate(game, beat, facts)
        await self.speak(text)
        return text

    async def nudge(self, game: MafiaGame) -> str:
        """Speak a short, theme-aware nudge WITHOUT appending to ``game.story``.

        Nudges fire repeatedly while a phase stalls. Treating them as story
        beats (via :meth:`narrate`) would both bloat the prompt sent on
        every future beat and desync the client's story log from what was
        actually spoken, since a nudge names no facts worth remembering.
        """
        parts = [
            f"Theme / setting (stay strictly consistent with it): {game.theme}",
            self._prompts.beats.get("nudge", ""),
            self._prompts.nudge_style,
        ]
        text = await self._one_shot("\n".join(p for p in parts if p))
        if not text:
            text = "Someone in the dark is taking their time."
        await self.speak(text)
        return text

    async def _one_shot(self, prompt: str) -> str:
        """Call the AI with a single user message and no tool loop (ADR-0010)."""
        if not isinstance(self._ai, AISamplingProvider):
            return ""
        try:
            response = await self._ai.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=prompt)],
                system_prompt=self._prompts.system,
                profile_name=self._ai_profile or None,
                tools_override=[],
            )
            return response.message.content.strip()
        except Exception:
            logger.exception("Mafia narration AI call failed")
            return ""
