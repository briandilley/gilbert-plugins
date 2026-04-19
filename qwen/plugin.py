"""Qwen plugin — registers the Alibaba Qwen (DashScope) AI backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class QwenPlugin(Plugin):
    """Side-effect plugin: importing the module registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="qwen",
            version="1.0.0",
            description="Qwen (Alibaba DashScope) AI backend",
            provides=["qwen_ai"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import qwen_ai  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return QwenPlugin()
