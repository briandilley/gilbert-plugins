"""Ngrok tunnel plugin — registers the NgrokTunnel backend with the tunnel service."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class NgrokPlugin(Plugin):
    """Side-effect plugin: importing ``ngrok_tunnel`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="ngrok",
            version="1.0.0",
            description="Ngrok tunnel backend (pyngrok-based)",
            provides=["ngrok_tunnel"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import ngrok_tunnel  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return NgrokPlugin()
