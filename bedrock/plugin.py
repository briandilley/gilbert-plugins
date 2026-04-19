"""AWS Bedrock plugin — registers the Bedrock Converse AI backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class BedrockPlugin(Plugin):
    """Side-effect plugin: importing the module registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="bedrock",
            version="1.0.0",
            description="AWS Bedrock AI backend (Converse API)",
            provides=["bedrock_ai"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import bedrock_ai  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return BedrockPlugin()
