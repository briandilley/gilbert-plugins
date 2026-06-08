"""llmfit plugin — registers the llmfit-backed host-resources backend.

``setup()`` side-effect-imports ``llmfit_host_resources`` so the ABC's
``__init_subclass__`` hook registers the ``LlmfitHostResources`` backend in
the ``HostResourcesBackend`` registry. The core ``HostResourcesService`` then
auto-prefers it over the built-in ``local`` backend when the ``llmfit`` binary
is present (see ``HostResourcesService._select_backend``).

The plugin also declares the ``llmfit`` binary as a :class:`RuntimeDependency`
so ``./gilbert.sh doctor`` verifies it (and can auto-install it — it's a small
self-contained CLI installable as a ``uv`` tool).
"""

from __future__ import annotations

from typing import Any

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    RuntimeDependency,
)

_LLMFIT = "llmfit"


class LlmfitPlugin(Plugin):
    """Registers the llmfit host-resources backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="llmfit",
            version="1.0.0",
            description=(
                "llmfit-backed host-resources probe — multi-vendor GPU "
                "detection (NVIDIA/AMD/Apple/Intel) for the hardware-fit feature"
            ),
            provides=["host_resources_llmfit"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import llmfit_host_resources  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass

    def runtime_dependencies(self, config: dict[str, Any] | None = None) -> list[RuntimeDependency]:
        """Declare the optional ``llmfit`` binary.

        Unconditional (no enablement gate): the host-resources service simply
        falls back to the built-in ``local`` probe when the binary is absent,
        so ``llmfit`` is a genuinely optional enhancement. The check
        *exercises* the tool (``llmfit --json system``) rather than probing a
        path, matching the convention used elsewhere. Auto-install is offered
        because it's a small, self-contained CLI with no system-wide side
        effects.
        """
        return [
            RuntimeDependency(
                name="llmfit",
                description=(
                    "Optional CLI that detects multi-vendor GPUs (NVIDIA, AMD, "
                    "Apple Silicon, Intel) and unified memory. When present, "
                    "Gilbert uses it for richer hardware-fit verdicts; when "
                    "absent, it falls back to the built-in NVIDIA-only probe."
                ),
                check_cmd=f"{_LLMFIT} --json system >/dev/null",
                install_hint=(
                    "Install with 'uv tool install llmfit' (also available via "
                    "'cargo install llmfit', Homebrew, or "
                    "https://github.com/AlexsJones/llmfit)."
                ),
                auto_install_cmd="uv tool install llmfit",
            )
        ]


def create_plugin() -> Plugin:
    return LlmfitPlugin()
