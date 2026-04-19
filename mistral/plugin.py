"""Mistral plugin — registers the Mistral La Plateforme AI backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class MistralPlugin(Plugin):
    """Side-effect plugin: importing the module registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="mistral",
            version="1.0.0",
            description="Mistral La Plateforme AI backend",
            provides=["mistral_ai"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import mistral_ai  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return MistralPlugin()
