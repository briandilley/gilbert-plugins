"""web-push plugin — registers the WebPush backend with the
PushNotificationBackend registry and contributes the subscribe panel."""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIPanel,
)


class WebPushPlugin(Plugin):
    """Side-effect plugin: importing ``web_push`` registers the backend.

    Also contributes a per-user ``account.extensions`` panel where users
    click "Enable browser notifications" to subscribe their browser.
    The panel reads the server's VAPID public key from the existing
    ``push.backends.list`` RPC (the backend surfaces it via
    :meth:`WebPush.runtime_data`) and registers the resulting browser
    PushSubscription via the generic ``push.routes.create`` RPC — no
    new service plumbing required.
    """

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="web-push",
            version="1.0.0",
            description=(
                "Browser/PWA push notifications via Web Push (VAPID)."
            ),
            provides=["web_push"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import web_push  # noqa: F401  — triggers backend registration

    async def teardown(self) -> None:
        pass

    def ui_panels(self) -> list[UIPanel]:
        return [
            UIPanel(
                panel_id="web_push.subscribe",
                slot="account.extensions",
                label="Browser notifications",
                description=(
                    "Subscribe this browser/device to Gilbert push "
                    "notifications."
                ),
                required_role="user",
            ),
        ]


def create_plugin() -> Plugin:
    return WebPushPlugin()
