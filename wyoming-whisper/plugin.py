"""Wyoming Whisper streaming transcription plugin."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class WyomingWhisperPlugin(Plugin):
    """Side-effect plugin: importing ``wyoming_whisper`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="wyoming-whisper",
            version="0.1.0",
            description="Local streaming STT via Wyoming-protocol Whisper",
            provides=["wyoming-whisper"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import wyoming_whisper  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return WyomingWhisperPlugin()
