from __future__ import annotations

from typing import Any

from gilbert_plugin_mafia.game import MafiaGame
from gilbert_plugin_mafia.narrator import Narrator


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


def _narrator(ai: Any = None, speaker: Any = None) -> Narrator:
    return Narrator(
        ai=ai,
        speaker=speaker,
        system_prompt="You are the narrator.",
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
