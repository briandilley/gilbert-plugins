# Gilbert Plugins

A collection of fun and useful plugins for the Gilbert AI assistant platform.

## Tech Stack

- **Language:** Python 3.12+
- **Framework:** Gilbert plugin system (see main repo CLAUDE.md for full architecture)
- **Testing:** pytest with mocks; tests live alongside each plugin at `<plugin>/tests/`
- **Dependencies:** Managed via the main Gilbert `uv` project — plugins import from `gilbert.*` interfaces

## Plugin Structure

Each plugin lives in its own directory with:

```
my-plugin/
    plugin.yaml      # manifest: name, version, provides, requires, depends_on, config
    plugin.py         # entry point: create_plugin() → Plugin instance
    __init__.py       # empty, makes it a package
    service.py        # service(s) implementing Service + ToolProvider
    ...               # additional modules as needed
```

### Key Interfaces

Plugins extend Gilbert by implementing these interfaces from the main repo:

- **`Plugin`** (`gilbert.interfaces.plugin`) — `metadata()`, `setup(context)`, `teardown()`
- **`Service`** (`gilbert.interfaces.service`) — `service_info()`, `start(resolver)`, `stop()`
- **`ToolProvider`** protocol (`gilbert.interfaces.tools`) — `tool_provider_name`, `get_tools()`, `execute_tool()`
- **`ToolOutput`** / **`UIBlock`** (`gilbert.interfaces.ui`) — return interactive forms from tools

### Plugin Registration

In `setup()`, plugins receive a `PluginContext` with:
- `context.services` — `ServiceManager` to register services
- `context.config` — resolved plugin config (plugin.yaml defaults merged with user overrides)
- `context.data_dir` — persistent data directory for the plugin
- `context.storage` — optional namespaced storage backend

### Configuration

Plugin config defaults go in `plugin.yaml` under `config:`. Users override in the main repo's `.gilbert/config.yaml` under `plugins.<plugin-name>`.

## Development Guidelines

- **Follow main repo conventions.** Interface-first, type hints everywhere, async I/O.
- **Depend on capabilities, not services.** Use `resolver.require_capability("music")`, not concrete class imports.
- **Return `ToolOutput` for interactive tools.** Use UI blocks (forms, buttons) for rich user interaction.
- **Write tests alongside the plugin.** Tests go in `<plugin>/tests/test_<feature>.py` with a sibling `conftest.py` that registers the plugin directory as `gilbert_plugin_<name>`. Gilbert's pyproject adds `std-plugins` to its `testpaths` so `uv run pytest` from the Gilbert repo root discovers them automatically.
- **Use relative imports within the plugin.** e.g., `from .game import GameState` — the plugin loader handles package setup.

## Commands

```bash
# Run all tests (from main repo root — picks up std-plugins/*/tests via pyproject testpaths)
uv run pytest

# Run a specific plugin's tests
uv run pytest std-plugins/arr/tests/ -v

# Type checking (from main repo root)
uv run mypy src/

# Linting
uv run ruff check std-plugins/
```

## Existing Plugins

- **guess-that-song** — Multiplayer music guessing game. Plays short clips on speakers, players guess via chat. AI-mediated with UI blocks for forms and action buttons.
