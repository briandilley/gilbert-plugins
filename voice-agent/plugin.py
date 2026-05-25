"""Voice-agent plugin — UI shell for the /voice route.

Backend orchestration lives in core ``VoicePipelineService``. This
plugin contributes only the SPA route + nav entry; it does not own a
service.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIRoute,
)


class VoiceAgentPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="voice-agent",
            version="0.2.0",
            description="Browser voice-conversation UI (rendered at /voice).",
            provides=["voice_agent_ui"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        # No service to register — the /voice route alone is the
        # plugin contribution. Backend orchestration lives in
        # ``core/services/voice_pipeline.py``.
        return None

    async def teardown(self) -> None:
        return None

    def ui_routes(self) -> list[UIRoute]:
        return [
            UIRoute(
                path="/voice",
                panel_id="voice_agent.page",
                label="Voice",
                description=(
                    "Start a real-time voice conversation with Gilbert."
                ),
                icon="mic",
                required_role="user",
                requires_capability="voice_pipeline",
                add_to_nav=True,
            ),
        ]


def create_plugin() -> Plugin:
    return VoiceAgentPlugin()
