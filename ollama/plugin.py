"""Ollama plugin — registers the local Ollama AI backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class OllamaPlugin(Plugin):
    """Side-effect plugin: importing the module registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="ollama",
            version="1.0.0",
            description="Local Ollama AI backend",
            provides=["ollama_ai"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import ollama_ai  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return OllamaPlugin()
