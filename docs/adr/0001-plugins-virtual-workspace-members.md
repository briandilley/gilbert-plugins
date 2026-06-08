# Plugins are uv virtual workspace members (`package = false`), loaded at runtime via importlib

Each plugin's `pyproject.toml` is marked `[tool.uv] package = false`. A single root `uv sync`
resolves and installs every plugin's third-party dependencies into the shared venv, but no wheel is
built — the plugin code itself is loaded dynamically at runtime via
`importlib.util.spec_from_file_location`. Every plugin must carry a `pyproject.toml` even with zero
dependencies, because the `members = ["std-plugins/*", …]` workspace glob errors on any subdirectory
that lacks one.

## Considered options

- **Build/install each plugin as a real wheel package** — rejected: loses hot-loadable, editable
  plugin directories and adds a build step to every plugin change.

## Consequences

There is no wheel-level isolation or independent versioning between plugins — they all share one
resolved dependency set in the parent venv.
