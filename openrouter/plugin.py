"""OpenRouter plugin — registers the OpenRouter AI backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class OpenRouterPlugin(Plugin):
    """Side-effect plugin: importing the module registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="openrouter",
            version="1.0.0",
            description="OpenRouter AI backend",
            provides=["openrouter_ai"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import openrouter_ai  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return OpenRouterPlugin()
