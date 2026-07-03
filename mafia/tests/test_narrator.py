from __future__ import annotations

from typing import Any

from gilbert_plugin_mafia.game import MafiaGame
from gilbert_plugin_mafia.narrator import NarrationPrompts, Narrator


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Resp:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _FakeAI:
    """Satisfies AISamplingProvider structurally (has_profile + complete_one_shot)."""

    def __init__(self, reply: str = "A grim tale unfolds.") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return _Resp(self.reply)


class _FakeSpeaker:
    def __init__(self) -> None:
        self.announced: list[tuple[str, Any, Any, str]] = []

    @property
    def backends(self) -> dict[str, Any]:
        return {}

    def get_backend(self, name: str) -> Any:
        return None

    async def resolve_names(self, names: list[str]) -> dict[str, str]:
        return {}

    async def announce(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        context: str = "",
    ) -> str:
        self.announced.append((text, speaker_names, volume, context))
        return "ok"


def _prompts() -> NarrationPrompts:
    return NarrationPrompts(
        system="You are the narrator.",
        beats={
            "intro": "intro beat",
            "night": "night beat",
            "dawn": "dawn beat",
            "dusk": "dusk beat",
            "nudge": "nudge beat",
            "win": "win beat",
        },
        narrate_style="Write 2-4 sentences.",
        nudge_style="Write one short sentence.",
        invent_theme="Invent a setting.",
    )


def _narrator(ai: Any = None, speaker: Any = None) -> Narrator:
    return Narrator(
        ai=ai,
        speaker=speaker,
        prompts=_prompts(),
        ai_profile="standard",
        speaker_names=["Kitchen"],
        volume=70,
    )


def _game() -> MafiaGame:
    g = MafiaGame(host_user_id="u1", host_name="Cam", theme="a camping trip")
    for i in range(4):
        g.add_player(f"P{i}")
    return g


async def test_narrate_appends_story_and_carries_context() -> None:
    ai = _FakeAI()
    n = _narrator(ai=ai)
    g = _game()
    g.story.append("Night one was quiet.")
    text = await n.narrate(g, beat="dawn", facts="P1, the doctor, was found dead.")
    assert text == "A grim tale unfolds."
    assert g.story[-1] == text
    call = ai.calls[0]
    assert call["tools_override"] == []          # ADR-0010
    assert call["system_prompt"] == "You are the narrator."
    user_msg = call["messages"][0].content
    assert "a camping trip" in user_msg           # theme for consistency
    assert "Night one was quiet." in user_msg     # story so far
    assert "P1, the doctor, was found dead." in user_msg
    assert "dawn beat" in user_msg                # configurable beat instruction
    assert "Write 2-4 sentences." in user_msg     # configurable style guidance


async def test_invent_theme_uses_configured_prompt() -> None:
    ai = _FakeAI(reply="A lighthouse cut off by a winter storm.")
    n = _narrator(ai=ai)
    await n.invent_theme()
    assert ai.calls[0]["messages"][0].content == "Invent a setting."


async def test_narrate_falls_back_without_ai() -> None:
    n = _narrator(ai=None)
    g = _game()
    text = await n.narrate(g, beat="dawn", facts="Nobody died last night.")
    assert text == "Nobody died last night."
    assert g.story[-1] == text


async def test_speak_uses_configured_speakers_and_never_raises() -> None:
    spk = _FakeSpeaker()
    n = _narrator(speaker=spk)
    await n.speak("Hello town")
    assert spk.announced[0][0] == "Hello town"
    assert spk.announced[0][1] == ["Kitchen"]
    n_none = _narrator(speaker=None)
    await n_none.speak("silence is fine")  # must not raise


async def test_invent_theme() -> None:
    ai = _FakeAI(reply="A lighthouse cut off by a winter storm.")
    n = _narrator(ai=ai)
    assert await n.invent_theme() == "A lighthouse cut off by a winter storm."


async def test_nudge_speaks_but_does_not_append_to_story() -> None:
    """M1: nudges must not bloat the prompt or desync the client's story log."""
    ai = _FakeAI(reply="Someone lingers in the shadows.")
    spk = _FakeSpeaker()
    n = _narrator(ai=ai, speaker=spk)
    g = _game()
    g.story.append("Night one was quiet.")
    story_before = list(g.story)

    text = await n.nudge(g)

    assert text == "Someone lingers in the shadows."
    assert g.story == story_before  # unchanged — not a story beat
    assert spk.announced[0][0] == text  # but it was spoken
    call = ai.calls[0]
    assert call["tools_override"] == []
    assert "a camping trip" in call["messages"][0].content  # still theme-aware


async def test_nudge_falls_back_without_ai() -> None:
    spk = _FakeSpeaker()
    n = _narrator(ai=None, speaker=spk)
    g = _game()
    text = await n.nudge(g)
    assert text == "Someone in the dark is taking their time."
    assert g.story == []
    assert spk.announced[0][0] == text
