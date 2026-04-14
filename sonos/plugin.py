"""Sonos plugin — registers the SonosSpeaker and SonosMusic backends."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class SonosPlugin(Plugin):
    """Side-effect plugin: importing the modules registers the backends."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="sonos",
            version="1.0.0",
            description="Sonos speaker + music backends",
            provides=["sonos_speaker", "sonos_music"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import sonos_music, sonos_speaker  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return SonosPlugin()
