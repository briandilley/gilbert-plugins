# `runtime_dependencies()` is enablement-aware (receives resolved config)

`Plugin.runtime_dependencies()` receives the **resolved plugin/backend config** so a plugin can
return a `RuntimeDependency` only when the relevant backend/service is *enabled*. `doctor` passes the
merged config it already loads (to locate plugin directories) into the hook, and reuses the "is this
backend/service enabled?" query from the enablement mechanism (core ADR-0018). So, e.g., the `ollama`
plugin advertises the Ollama-daemon dependency — with its install hint — **only when the ollama
backend is enabled**; an Anthropic-only operator who runs `doctor` is not told to install Ollama they
don't use. This evolves ADR-0003.

## Considered options

- **Unconditional checks** (today's behavior — every discovered plugin's deps always checked) —
  rejected: FAILs and install hints for *disabled* backends are noise that erodes trust in the
  `doctor` report and contradicts "when that backend is enabled."

## Consequences

- The `runtime_dependencies()` signature changes to take the resolved config. `doctor` instantiates
  plugins *without booting Gilbert* (no `PluginContext`), so config is passed **explicitly** to the
  hook rather than read from a context.
- Existing overrides (browser, tesseract, kokoro, …) must be updated to accept (and may ignore) the
  config parameter — there is no free backward-compat here; the migration is mechanical, and the
  root + std-plugins `CLAUDE.md` runtime-dependency examples must be updated in the same change.
