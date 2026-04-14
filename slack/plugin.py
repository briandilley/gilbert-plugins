"""Slack plugin — registers the Slack Socket Mode bot as a service."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class SlackPlugin(Plugin):
    """Registers SlackService (a Socket Mode bot) with the service manager."""

    def __init__(self) -> None:
        self._service: object | None = None

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="slack",
            version="1.0.0",
            description="Slack integration (Socket Mode)",
            provides=["slack"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .slack_service import SlackService

        self._service = SlackService()
        context.services.register(self._service)

    async def teardown(self) -> None:
        pass  # Service lifecycle handled by ServiceManager.stop_all()


def create_plugin() -> Plugin:
    return SlackPlugin()
