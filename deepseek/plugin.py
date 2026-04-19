"""DeepSeek plugin — registers the DeepSeek AI backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class DeepSeekPlugin(Plugin):
    """Side-effect plugin: importing the module registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="deepseek",
            version="1.0.0",
            description="DeepSeek AI backend",
            provides=["deepseek_ai"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import deepseek_ai  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return DeepSeekPlugin()
