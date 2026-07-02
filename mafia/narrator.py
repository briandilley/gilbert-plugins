"""Narration engine: themed, story-consistent beats via one-shot AI calls.

Killer identities are never given to the model — only public facts
(deaths, revealed characters) plus the running story. AI calls use
complete_one_shot(tools_override=[]) per core ADR-0010; chat() would
persist a conversation per beat.
"""

from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole
from gilbert.interfaces.speaker import SpeakerProvider

from .game import MafiaGame

logger = logging.getLogger(__name__)

_MAX_STORY_LINES = 20  # cap prompt growth on long games

_BEAT_INSTRUCTIONS: dict[str, str] = {
    "intro": "Open the game: set the scene, name the players as inhabitants, and tell everyone a killer walks among them. End by telling everyone to close their eyes as night falls.",
    "night": "Briefly narrate night falling. Everyone's eyes are closed.",
    "dawn": "Narrate the morning discovery described in the facts, then tell everyone to open their eyes.",
    "dusk": "Everyone has closed their eyes after the vote. Narrate the outcome described in the facts as part of the story.",
    "nudge": "One short in-character line gently hurrying a hesitating, unnamed someone in the dark. Do not name anyone.",
    "win": "Narrate the finale described in the facts, then congratulate the winners and reveal nothing else.",
}


class Narrator:
    """Builds prompts, calls the AI, speaks the result. All I/O degrades gracefully."""

    def __init__(
        self,
        *,
        ai: Any,
        speaker: Any,
        system_prompt: str,
        ai_profile: str,
        speaker_names: list[str] | None,
        volume: int | None,
    ) -> None:
        self._ai = ai
        self._speaker = speaker
        self._system_prompt = system_prompt
        self._ai_profile = ai_profile
        self._speaker_names = speaker_names
        self._volume = volume

    async def invent_theme(self) -> str:
        """One-shot ask the AI for a 1-sentence murder-mystery setting."""
        text = await self._one_shot(
            "Invent an evocative setting for a murder-mystery party game. "
            "Reply with a single short sentence describing the setting and nothing else."
        )
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
            _BEAT_INSTRUCTIONS.get(beat, ""),
            "Write 2-4 sentences. Spoken aloud, so no stage directions or markdown.",
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

    async def _one_shot(self, prompt: str) -> str:
        """Call the AI with a single user message and no tool loop (ADR-0010)."""
        if not isinstance(self._ai, AISamplingProvider):
            return ""
        try:
            response = await self._ai.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=prompt)],
                system_prompt=self._system_prompt,
                profile_name=self._ai_profile or None,
                tools_override=[],
            )
            return response.message.content.strip()
        except Exception:
            logger.exception("Mafia narration AI call failed")
            return ""
