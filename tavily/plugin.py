"""Tavily web search plugin — registers the TavilySearch backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class TavilyPlugin(Plugin):
    """Side-effect plugin: importing ``tavily_search`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="tavily",
            version="1.0.0",
            description="Tavily web search backend",
            provides=["tavily_search"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import tavily_search  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return TavilyPlugin()
