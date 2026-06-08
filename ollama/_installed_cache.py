"""Process-global cache of installed Ollama tags.

The set of model tags installed in the Ollama daemon is **host-global** —
it describes the one daemon this Gilbert process talks to, not anything
per-user or per-request — so a plain module-level list is the right shape
(multi-user isolation, core ADR-0009, doesn't apply here). Two collaborators
share it:

- ``OllamaAI`` (the AI backend) reads it in the *synchronous*
  ``available_models()`` and writes it after fetching ``/api/tags`` in
  ``initialize()`` / ``refresh_installed_models()``.
- ``OllamaRuntimeService`` (the ``local_model_runtime`` capability) writes
  it after a successful ``pull_model`` / ``delete_model`` so the change is
  reflected in ``available_models()`` *immediately*, without a backend
  restart — which is what makes a freshly-pulled model chat-selectable on
  the next picker read.

Living in a shared module (rather than on either instance) lets the two
collaborators — wired up independently by the plugin loader — stay in sync
without one reaching into the other's private state.
"""

from __future__ import annotations

# Host-global, not per-user/request: the tags installed in the local Ollama
# daemon. Mutated wholesale via ``set()`` (never appended to in place) so a
# reader either sees the old list or the new one, never a half-written one.
_INSTALLED_TAGS: list[str] = []


def get() -> list[str]:
    """Return a copy of the currently-cached installed tags."""
    return list(_INSTALLED_TAGS)


def set(tags: list[str]) -> None:
    """Replace the cached installed tags with ``tags`` (a defensive copy)."""
    global _INSTALLED_TAGS
    _INSTALLED_TAGS = list(tags)
