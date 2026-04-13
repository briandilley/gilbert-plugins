"""Radarr + Sonarr plugin — manages movies and TV shows via the *arr APIs."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class ArrPlugin(Plugin):
    """Registers Radarr and Sonarr services."""

    def __init__(self) -> None:
        self._radarr: object | None = None
        self._sonarr: object | None = None

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="arr",
            version="1.0.0",
            description="Radarr + Sonarr integration for movies and TV shows",
            provides=["radarr", "sonarr"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .radarr_service import RadarrService
        from .sonarr_service import SonarrService

        self._radarr = RadarrService()
        self._sonarr = SonarrService()
        context.services.register(self._radarr)
        context.services.register(self._sonarr)

    async def teardown(self) -> None:
        pass  # Service lifecycle handled by ServiceManager.stop_all()


def create_plugin() -> Plugin:
    """Entry point called by the plugin loader."""
    return ArrPlugin()
