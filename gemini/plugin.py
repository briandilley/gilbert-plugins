"""Gemini plugin — registers the Google Gemini AI backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class GeminiPlugin(Plugin):
    """Side-effect plugin: importing the module registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="gemini",
            version="1.0.0",
            description="Google Gemini AI backend",
            provides=["gemini_ai"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import gemini_ai  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return GeminiPlugin()
