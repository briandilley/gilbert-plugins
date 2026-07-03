# Plugins

First-party integrations for Gilbert. This repo (`gilbert-plugins`) is mounted as the
`std-plugins/` submodule inside a Gilbert checkout; each subdirectory is one plugin. This glossary
covers the vocabulary unique to *authoring and packaging* a plugin. Shared platform terms
(**Backend**, **Service**, **Capability**, **ConfigParam**, **tool**, **Role**) are defined in the
[Core glossary](../src/gilbert/CONTEXT.md); see [`CONTEXT-MAP.md`](../CONTEXT-MAP.md).

## Anatomy of a plugin

**Plugin**:
A self-contained Gilbert extension in one directory, loaded at startup. It exposes
`create_plugin()` returning a `Plugin` subclass with `metadata()` / `setup()` / `teardown()` and
optional hooks. Distinct from the *Backend* it registers (the concrete integration) and any
*Service* it registers (a discoverable runtime component).

**Plugin context** (`PluginContext`):
The object handed to `setup()` carrying the service manager, the initial resolved config (a
snapshot — *not* for runtime reads), a per-plugin data directory, and a storage handle pre-scoped
to the plugin. See *namespaced storage*.

**Namespaced storage**:
The plugin-scoped storage handle (`context.storage`) auto-prefixed to the
`gilbert.plugin.<plugin-name>` collection namespace, so a plugin's entity collections never collide
with core's or another plugin's.

**provides / requires / depends_on**:
Manifest fields (in `plugin.yaml` / `PluginMeta`). `provides` lists the backend or service names the
plugin contributes; `requires` / `depends_on` declare what it needs. "Provides" is also the column
in the README plugin table.

**Virtual workspace member**:
A plugin whose `pyproject.toml` is marked `package = false`: uv resolves and installs its
third-party dependencies into the shared venv but never builds a wheel, and the plugin code is
loaded dynamically at runtime instead of as an installed package.
See [ADR-0001](./docs/adr/0001-plugins-virtual-workspace-members.md).
_Avoid_: package, wheel (a plugin is neither).

**Side-effect import**:
The deliberately "unused" `from . import my_backend  # noqa: F401` in `setup()` whose only job is to
trigger the backend's registration in the [Core backend registry](../src/gilbert/CONTEXT.md). It is
load-bearing — deleting it unregisters the backend.

## Runtime requirements

**Runtime dependency**:
A non-pip, OS-level prerequisite a plugin needs (Chromium, tesseract, ffmpeg, system fonts),
declared as a `RuntimeDependency` with a check command and install hint. Distinct from the Python
dependencies in `pyproject.toml`.

**doctor**:
The `./gilbert.sh doctor` command that runs every plugin's runtime-dependency checks and reports
PASS/FAIL with hints (`--install` runs opt-in auto-installs). A good check *exercises* the dep
(actually launches headless Chromium) rather than probing a file path.
See [ADR-0003](./docs/adr/0003-runtime-dependencies-doctor.md).

**toggleable plugin**:
A plugin shipped disabled by default (e.g. browser); its nav entries, slash commands, RPCs, and
tools come online only once an admin enables its service.

## Frontend & commands

**UI panel vs. slot**:
A *slot* is a named extension point a page exposes (built-ins: `account.extensions`,
`settings.<category>`, `header.widgets`, `dashboard.top`/`.bottom`). A *UI panel* is a plugin
component that mounts *into* a slot, filtered by role. Plugins may declare their own slots too.

**UI route**:
A full SPA page a plugin owns (its own path, optional nav entry / dashboard card, gateable on a
service capability). Contrast with a *panel*, which only mounts into someone else's slot.

**panel_id**:
The string key binding a backend `UIPanel` / `UIRoute` declaration to the SPA component registered
under the same id. No matching registration (e.g. a backend-only load) → the panel is silently
skipped.
See [ADR-0002](./docs/adr/0002-plugins-ship-own-frontend.md).

**Slash namespace**:
The short user-facing prefix for a service's slash commands (e.g. `/radio.*`), set via
`slash_namespace`. Without it, the namespace defaults to the (often long) plugin directory name.

## Local model management

**Model catalog**:
The browseable set of installable open-weight models surfaced for local use, sourced from the
Hugging Face Hub and enriched with a Gilbert-curated *recommended overlay*. Distinct from the
*enabled models* a backend advertises to the chat UI, and from the **backend registry** (reserve
"registry" for that).
_Avoid_: model list, model registry.

**Recommended overlay**:
The small, Gilbert-maintained set of vetted, tool-capable models layered on top of the raw Hugging
Face catalog to badge known-good choices and default-sort them ahead of the long tail. The
successor to the `ollama` backend's hand-curated static model list.
_Avoid_: whitelist, allowlist (it ranks, it doesn't gate).

**Hardware fit**:
The manager's pre-pull estimate of whether a given model + quantization will run, derived from its
per-quant size × an overhead factor (KV cache / context) compared against [[host-resources]]:
*fits-VRAM* (fast) / *fits-RAM* (slow, CPU or partial offload) / *won't-fit* / *unknown* (remote
runtime). Policy, computed in the manager — not in core, which only supplies the raw host data.
Surfaced as a *filter* ("Compatible"), not as a ranking weight: the browser shows all models and
sorts by Hugging Face's own signals.
_Avoid_: compatibility, can-run.

**Local model runtime** (capability `LocalModelRuntimeProvider`):
The abstraction over something that can `list` / `pull` / `delete` locally-served open-weight models
and report its `base_url` — implemented by the `ollama` plugin, consumed by the manager. The
protocol lives in core `interfaces/`; it lets the manager drive installs without reading the runtime
backend's config, and lets a future runtime stand in for Ollama unchanged.
_Avoid_: model store, downloader.

## Games

**Player**:
A participant in a single game, identified only for that game's duration. Not the same as a
*User* (account holder) — a Player may have no account. Creating a game always requires a User.
_Avoid_: user, guest (both mean something else platform-wide).

**Join code**:
The short code (or QR) a game's creator shares that admits people as Players of that specific game.

**Character**:
A Player's secret in-game identity in Mafia — citizen, killer, doctor, or detective.
_Avoid_: role (reserved platform-wide for the RBAC tier: admin/user/everyone).

**Host**:
The User who created a game. The Host is also a normal Player, but additionally steers the game's
pacing (skipping a stalled phase, ending an undecided Day, removing a departed Player, aborting).
_Avoid_: owner, admin (RBAC terms), narrator (that's Gilbert).

**Narrator**:
Gilbert's storytelling persona that runs a Mafia game: announces each phase aloud and weaves the
night's outcome into a continuing story. A death is narrated as *who* died, never their Character
(roles stay hidden until the game ends).

**Ghost**:
A dead Player. Ghosts spectate with full knowledge of all hidden state, may not speak in the room,
and no longer act or vote.

**Theme**:
The story setting for a Mafia game (a camping trip, a haunted mansion, …), chosen by the game's
creator at creation — from presets, free text, or left for the Narrator to invent — and held
consistent across the whole game's narration.

**Night / Day**:
The two alternating Mafia phases. At *Night* every living Player acts at the same time on their own
phone — there is no eyes-closed sequence: killers pick a target (a two-killer team sees each other's
live pick and locks in on agreement), the doctor protects, the detective investigates, and everyone
else taps Next. The Night resolves once all have submitted. By *Day* the town discusses and votes to
eliminate a suspect; a vote eliminates only on a strict majority of living Players.

## Vendor-specific terms

**SMAPI service number**:
The Sonos Music API service identifier embedded in a DIDL descriptor (`sn`, e.g. 2311 world /
3079 US) that routes a Spotify URI to the right service over legacy UPnP SOAP. Explicitly *not* the
broken Cloud Control API path.
See [ADR-0005](./docs/adr/0005-spotify-on-sonos-smapi-soap-bridge.md).
