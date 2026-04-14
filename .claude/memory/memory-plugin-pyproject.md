# Plugin pyproject.toml Is Mandatory

## Summary
Every plugin subdirectory under `std-plugins/` (and `local-plugins/`, `installed-plugins/`) must have a `pyproject.toml`, even if the plugin has zero third-party Python dependencies. Gilbert's root `pyproject.toml` glob (`[tool.uv.workspace] members = ["std-plugins/*", ...]`) expects every matched directory to have one, and `uv sync` errors out cleanly with "Workspace member X is missing a pyproject.toml" if even one is absent.

## Details

### Minimal valid pyproject.toml

```toml
[project]
name = "gilbert-plugin-<name>"
version = "1.0.0"
description = "One-liner"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
package = false
```

Key pieces:

- **`name`** — Must be unique across all workspace members. Convention is `gilbert-plugin-<dirname>`. Gilbert's root pyproject references it under `[tool.uv.sources]` with `{ workspace = true }`.
- **`version`** — Required, any semver-ish string works.
- **`dependencies = []`** — If the plugin uses only things already in Gilbert's core deps (`httpx`, `aiohttp`, `pillow`, `pyyaml`, etc.), leave this empty with a comment. Don't omit the key.
- **`[tool.uv] package = false`** — Marks the plugin as a virtual workspace member. Without this, uv tries to build the plugin as a wheel, fails because the plugin directory isn't structured as a Python package (has non-Python files, test subdirectories, etc.), and errors the whole sync.

### Why package = false

Plugins are loaded dynamically via `importlib.util.spec_from_file_location` at runtime — they're NOT installed into site-packages as importable top-level packages. `package = false` tells uv to treat the workspace member as a dependency manifest only: resolve and install `dependencies` into the shared venv, but don't try to build a wheel out of the plugin directory itself.

### Why the empty-deps rule matters

If you forget to create a `pyproject.toml` (or remove one during a refactor), the next `uv sync` anywhere in the Gilbert tree will fail with:

```
error: Workspace member `/.../std-plugins/<name>` is missing a `pyproject.toml` (matches: `std-plugins/*`)
```

This is **not** a glob-member-skipped situation — uv hard-errors. So the invariant is: every plugin directory matched by the workspace glob must have a `pyproject.toml`, full stop.

### Runtime-installed plugins

When a plugin is installed via `/plugin install <url>` at runtime, `PluginManagerService` reads the plugin's `pyproject.toml` and checks whether `[project].dependencies` is non-empty:

- **Empty deps** → hot-load, no restart needed.
- **Non-empty deps** → persist install with `needs_restart=True`, defer loading. `/plugin restart` exits Gilbert with code 75, `gilbert.sh`'s supervisor loop re-runs `uv sync` (which pulls the new deps because the plugin dir is now a workspace member), and relaunches. On next boot the plugin loads and `reconcile_loaded_plugins()` clears the flag.

Plugins installed at runtime **must** have a `pyproject.toml` at the root of their plugin directory, or the next `uv sync` will fail and block the supervisor from relaunching Gilbert.

## Related
- Root `pyproject.toml` in Gilbert — `[tool.uv.workspace]` + `[tool.uv.sources]`
- `README.md` — "Adding a plugin" section shows the full template
- `CLAUDE.md` — plugin development guidelines and the runtime-install flow
