"""Guess That Song plugin — multiplayer music guessing game."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class GuessThatSongPlugin(Plugin):
    """Registers the Guess That Song game service."""

    def __init__(self) -> None:
        self._service: object | None = None

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="guess-that-song",
            version="1.0.0",
            description="Guess That Song — multiplayer music guessing game",
            provides=["guess_game"],
            requires=["music", "speaker_control"],
        )

    async def setup(self, context: PluginContext) -> None:
        from .service import GuessGameService

        self._service = GuessGameService(config=context.config)
        context.services.register(self._service)

    async def teardown(self) -> None:
        pass  # Service lifecycle handled by ServiceManager.stop_all()


def create_plugin() -> Plugin:
    """Entry point called by the plugin loader."""
    return GuessThatSongPlugin()
