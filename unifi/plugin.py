"""UniFi plugin — registers presence and doorbell backends against Ubiquiti gear.

Covers UniFi Network (Wi-Fi clients), UniFi Protect (camera face detection
and doorbell events), and UniFi Access (badge readers) — aggregated into a
single presence backend plus a separate doorbell backend.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class UniFiPlugin(Plugin):
    """Side-effect plugin: importing the modules registers the backends."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="unifi",
            version="1.0.0",
            description="UniFi presence + doorbell backends",
            provides=["unifi_presence", "unifi_doorbell"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        # Importing these modules triggers backend registration via
        # __init_subclass__ on PresenceBackend / DoorbellBackend.
        from . import doorbell, presence  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return UniFiPlugin()
