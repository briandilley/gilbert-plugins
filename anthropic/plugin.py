"""Anthropic plugin — registers Claude-based AI and Vision backends."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class AnthropicPlugin(Plugin):
    """Side-effect plugin: importing the modules registers the backends."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="anthropic",
            version="1.0.0",
            description="Anthropic Claude AI and Vision backends",
            provides=["anthropic_ai", "anthropic_vision"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import anthropic_ai, anthropic_vision  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return AnthropicPlugin()
