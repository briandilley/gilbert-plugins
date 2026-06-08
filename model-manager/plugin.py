"""Model-manager plugin — registers the local-model manager service + page.

``setup()`` constructs ``ModelManagerService`` and registers it as a
discoverable service. The plugin declares one ``UIRoute`` — the ``/models``
admin page — gated on the ``model_manager`` capability so the nav entry and
the SPA route both disappear when the manager isn't running (e.g. because
its enablement dependency on the Ollama backend is unmet, or the operator
toggled it off under Settings → Services).

The manager reaches the local runtime only through the
``local_model_runtime`` capability (advertised by the ollama plugin), so it
never reads the AI backend's config — see ADR-0007.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta, UIRoute


class ModelManagerPlugin(Plugin):
    """Registers the model-manager service and its full-page Models UI."""

    def __init__(self) -> None:
        self._service: object | None = None

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="model-manager",
            version="1.0.0",
            description=(
                "Local model manager — browse the Hugging Face GGUF catalog "
                "(HF-native sort/search + a Recommended overlay) and list "
                "installed models via Ollama."
            ),
            provides=["model_manager"],
            requires=["local_model_runtime"],
        )

    async def setup(self, context: PluginContext) -> None:
        from .model_manager_service import ModelManagerService

        self._service = ModelManagerService()
        context.services.register(self._service)

    async def teardown(self) -> None:
        pass  # Service lifecycle handled by ServiceManager.stop_all()

    def ui_routes(self) -> list[UIRoute]:
        return [
            UIRoute(
                path="/models",
                panel_id="model-manager.page",
                label="Models",
                description=(
                    "Browse the Hugging Face GGUF catalog and list the "
                    "models installed in the local runtime (Ollama)."
                ),
                icon="package",
                required_role="admin",
                # Gate the route + nav entry on the manager's own
                # capability so disabling it under Settings → Services (or
                # the unmet Ollama enablement dependency) hides both the
                # nav link and the SPA route.
                requires_capability="model_manager",
                add_to_nav=True,
                nav_parent_group="system",
            ),
        ]


def create_plugin() -> Plugin:
    """Entry point called by the plugin loader."""
    return ModelManagerPlugin()
