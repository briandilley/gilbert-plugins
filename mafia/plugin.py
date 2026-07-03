from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta, UIRoute


class MafiaPlugin(Plugin):
    """Registers the MafiaService and the /mafia SPA page."""

    def __init__(self) -> None:
        self._service: object | None = None

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="mafia",
            version="1.0.0",
            description="In-person Mafia party game narrated by Gilbert",
            provides=["mafia_game"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .service import MafiaService

        service = MafiaService()
        context.services.register(service)
        self._service = service

    async def teardown(self) -> None:
        self._service = None

    def ui_routes(self) -> list[UIRoute]:
        return [
            UIRoute(
                path="/mafia",
                panel_id="mafia.page",
                label="Mafia",
                description="Social-deduction party game narrated by Gilbert",
                icon="venetian-mask",
                required_role="everyone",
                requires_capability="mafia_game",
                add_to_nav=True,
                # Nests under the top-level "Games" group (declared in
                # web_api.py). The group itself supplies the dashboard
                # card landing on this route, so — like Andon FM under
                # Media — this route doesn't also set show_in_dashboard,
                # which would render a duplicate card.
                nav_parent_group="games",
            )
        ]


def create_plugin() -> Plugin:
    return MafiaPlugin()
