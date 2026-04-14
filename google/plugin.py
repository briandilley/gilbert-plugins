"""Google plugin — registers Google OAuth, Workspace directory, Gmail, and Drive backends."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class GooglePlugin(Plugin):
    """Side-effect plugin: importing the modules registers the backends.

    Covers:
    - ``google_auth`` — AuthBackend (OAuth ID token verification)
    - ``google_directory`` — UserProviderBackend (Google Workspace directory)
    - ``gmail`` — EmailBackend
    - ``gdrive_documents`` — DocumentBackend (Google Drive)
    """

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="google",
            version="1.0.0",
            description="Google integration suite (auth, directory, Gmail, Drive)",
            provides=["google_auth", "google_directory", "gmail", "google_drive"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import (  # noqa: F401
            gdrive_documents,
            gmail,
            google_auth,
            google_directory,
        )

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return GooglePlugin()
