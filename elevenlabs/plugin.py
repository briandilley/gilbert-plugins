"""ElevenLabs TTS plugin — registers the ElevenLabsTTS backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class ElevenLabsPlugin(Plugin):
    """Side-effect plugin: importing ``elevenlabs_tts`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="elevenlabs",
            version="1.0.0",
            description="ElevenLabs TTS backend",
            provides=["elevenlabs_tts"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import elevenlabs_tts  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return ElevenLabsPlugin()
