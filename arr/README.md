# arr — Radarr + Sonarr plugin for Gilbert

Manage your movie and TV libraries from Gilbert chat. Adds one service for
[Radarr](https://radarr.video/) and one for [Sonarr](https://sonarr.tv/),
each exposing search, add, remove, queue, calendar, and history as AI
tools plus terse slash commands. Includes an interactive name-based
picker (`/radarr.find` / `/sonarr.find`) that renders candidate cards
with posters and a one-click "Add" button.

## What it gives you

- **21 tools** split across Radarr (10) and Sonarr (11) — search, list,
  details, upcoming, queue, recent, profiles, add, remove, grab, and
  (Sonarr only) episodes.
- **Slash namespaces** `radarr.*` and `sonarr.*` so everything
  autocompletes cleanly from the chat input without polluting the
  top-level command space.
- **Interactive picker** — `radarr_find` / `sonarr_find` return a
  `ToolOutput` with one `UIBlock` per candidate (up to five), each
  carrying a ~96 px poster thumbnail, the candidate's metadata, and a
  single **Add to Radarr** / **Add to Sonarr** button whose value is
  the TMDB / TVDB id. Clicking it surfaces to the AI as an unambiguous
  submission that triggers the underlying `radarr_add` / `sonarr_add`
  tool with the right id.
- **Live-service dropdowns** — `default_quality_profile` and
  `default_root_folder` config fields are populated by hitting the real
  Radarr / Sonarr `/qualityprofile` and `/rootfolder` endpoints on
  service start (and on every successful Test Connection), so the
  Settings UI shows real options instead of a free-text input.
- **Test connection action** in the Settings UI that pings
  `/api/v3/system/status` and reports `appName` + version plus the
  refreshed profile / folder counts.
- **RBAC-split tool roles** — read ops (`search`, `list`, `details`,
  `find`, `upcoming`, `queue`, `recent`, `profiles`, `episodes`) are
  `user`, write ops (`add`, `remove`, `grab`) are `admin`. Slash
  autocomplete hides admin-only commands from non-admin chat users.

## Prerequisites

- A running Radarr and/or Sonarr server that the Gilbert host can
  reach over HTTP. The plugin uses the v3 API (`/api/v3`), which is
  standard on all supported releases.
- The API key from each server — in Radarr / Sonarr, **Settings →
  General → Security → API Key**.
- Gilbert with this directory present under `plugins/arr/` and
  `plugins.directories: [plugins]` in `gilbert.yaml` (the default as
  of the commit that ships this plugin). Each service is opt-in and
  starts disabled.

## Installation

```bash
# From the Gilbert repo root
cd plugins
git clone git@github.com:briandilley/gilbert-plugins.git .   # if the
# plugins directory is empty; otherwise the arr tree just needs to
# exist under plugins/arr/
```

No pip install step — the plugin imports only from `gilbert.interfaces.*`
and `httpx`, which Gilbert already depends on. Restart Gilbert after
adding the plugin so the manifest scanner picks it up.

## Configuration

Every runtime setting lives in entity storage and is managed from the
web UI at **Settings → Services** (for the on/off toggles) and
**Settings → Media** (for each service's config section). Nothing
needs to go in `config.yaml`.

### Services tab

After the plugin loads, two new toggles appear under **Services**:

- **Radarr — movie library management**
- **Sonarr — TV show library management**

Both default to **off**. Flip one on and its config section appears
under **Media** on the next render.

### Radarr / Sonarr config section

Each service declares the following parameters. They live under the
`radarr` and `sonarr` entity storage namespaces respectively.

| Key | Type | Required | Restart | Default | Notes |
|---|---|---|---|---|---|
| `enabled` | bool | yes | yes | `false` | Master toggle. Also surfaced on the Services tab. |
| `url` | string | yes | yes | `""` | Base URL, e.g. `http://radarr.local:7878`. |
| `api_key` | string (sensitive) | yes | yes | `""` | Copied from Radarr/Sonarr Settings → General. Masked in the UI. |
| `default_quality_profile` | string (dropdown) | no | no | `""` | Used when `radarr_add` / `sonarr_add` is called without an explicit profile. Empty falls back to the first profile configured on the server. Populated from the live server once the service is up. |
| `default_root_folder` | string (dropdown) | no | no | `""` | Overrides the root folder path for new additions. Empty uses the first root folder configured on the server. Populated from the live server. |

The `url` and `api_key` fields are `restart_required`, so saving them
restarts the service which fetches the profile and root folder lists
in one round trip.

### Test connection action

Next to the config section there's a **Test connection** button
(`admin`-only). It hits `GET /api/v3/system/status` and:

- on success — toasts `Connected to Radarr 5.4.1.8668 — 3 quality profile(s), 2 root folder(s).` and also refreshes the two dropdowns so any new profiles or root folders you added in Radarr show up without restarting Gilbert;
- on `401`/`403` — toasts `Radarr API error: API key rejected`;
- on other HTTP errors — toasts `Radarr API error: HTTP {code}`;
- on network errors — toasts `Connection failed: {exception}`.

## Slash commands

Every tool is exposed as a slash command under the `radarr.` or
`sonarr.` namespace so they autocomplete as you type `/r…` or `/s…`.
Arguments follow `shlex` quoting — use `"..."` around values that
contain spaces.

### Radarr

| Command | Role | Tool | Notes |
|---|---|---|---|
| `/radarr.search <query>` | user | `radarr_search` | Text-only catalog lookup (returns up to 5 candidates with TMDB ids). Use this when the AI needs the numeric id for a follow-up call. |
| `/radarr.find <query>` | user | `radarr_find` | **Interactive picker** — search by name and get one UI block per candidate with a poster and an **Add to Radarr** button. Prefer this for chat users. |
| `/radarr.list` | user | `radarr_list` | Up to 30 monitored movies sorted by title, with download state. |
| `/radarr.details <name or id>` | user | `radarr_details` | Details for a movie already in the library. Accepts a partial-name match or a numeric Radarr id. |
| `/radarr.upcoming` | user | `radarr_upcoming` | Calendar of releases from 30 days ago to 90 days ahead. |
| `/radarr.queue` | user | `radarr_queue` | Current download queue with progress %. |
| `/radarr.recent [limit]` | user | `radarr_recent` | Most recently imported movies. `limit` 1–10, default 5. `/radarr.recent 1` = "the last one downloaded". |
| `/radarr.profiles` | user | `radarr_profiles` | Lists quality profiles with ids — useful before calling `/radarr.add`. |
| `/radarr.add <tmdb_id> [quality_profile] [monitored]` | admin | `radarr_add` | Adds a movie by TMDB id. `quality_profile` is a name (partial match) or id and falls back to `default_quality_profile`. `monitored` defaults to `true`. |
| `/radarr.remove <name or id>` | admin | `radarr_remove` | Deletes the movie **and its files** from Radarr. |
| `/radarr.grab <name or id>` | admin | `radarr_grab` | Triggers a Radarr download search for an existing movie (useful for upgrading or retrying a missing release). |

### Sonarr

Same shape, same roles, with `sonarr_` tools. Additions:

| Command | Role | Tool | Notes |
|---|---|---|---|
| `/sonarr.episodes <name or id>` | user | `sonarr_episodes` | Episode stats for a show — latest aired, latest downloaded, total/have/missing counts, plus the five most recent episodes. |
| `/sonarr.add <tvdb_id> [quality_profile] [monitored]` | admin | `sonarr_add` | Adds a show by TVDB id. `monitored=true` also searches for missing episodes. |

## Interactive add flow

Typical user flow — no knowledge of TMDB or TVDB ids required:

```
user> /radarr.find inception

Gilbert> Found 2 match(es) for 'inception' — click Add to Radarr on
         the one you want:
         - Inception (2010) — 148min
         - Inception 2: The Sequel (1998) — 92min

[UIBlock 1]
┌──────────────────────────────┐
│ Add Inception (2010) to      │
│ Radarr?                      │
│                              │
│  [poster thumbnail]          │
│                              │
│  Inception (2010)            │
│  148 min                     │
│  TMDB id: 27205              │
│                              │
│  A thief who steals…         │
│                              │
│  [ Add to Radarr ]           │
└──────────────────────────────┘

[UIBlock 2]
… second candidate …
```

Clicking **Add to Radarr** ships the tmdb_id as the form's only value:
the form submission text Gilbert forwards to the AI reads

```
[Brian submitted: Add Inception (2010) to Radarr?]
- tmdb_id: 27205
```

which is unambiguous enough for the AI to call `radarr_add(tmdb_id=27205)`
without guessing. Candidates already in the library render as a card
with a poster and the note *"Already in your library."* but no button.

The Sonarr flow is identical — the form element is named `tvdb_id` and
the button routes to `sonarr_add`.

## Using from the AI

Every tool is also directly AI-callable. The most common patterns:

- **Find + add by name**: `radarr_find(query="dune")` then (AI decides)
  `radarr_add(tmdb_id=438631)`. Prefer this for user-initiated adds —
  the UI picker makes disambiguation trivial.
- **Programmatic add**: `radarr_search` to get candidates, then
  `radarr_add(tmdb_id=..., quality_profile="Ultra-HD", monitored=true)`.
- **Name-based queries**: `radarr_details(movie="the matrix")` and
  `radarr_remove(movie="the matrix")` accept a partial name or a
  numeric Radarr id — the service does the lookup so the AI doesn't
  have to shuffle ids through the conversation.
- **Queue and calendar**: `radarr_queue`, `radarr_upcoming`, and
  `radarr_recent(limit=1)` are useful read-only tools the AI can chain
  to answer "what's downloading?", "what's coming out?", and "what did
  I watch last?".

## Architecture

```
plugins/arr/
    __init__.py             # empty, makes it a Python package
    plugin.yaml             # manifest — name, version, provides
    plugin.py               # Plugin subclass; registers both services
    arr_client.py           # shared async httpx wrapper (v3 API + X-Api-Key)
    radarr_service.py       # RadarrService (Service + ToolProvider + Configurable + ConfigActionProvider)
    sonarr_service.py       # SonarrService (same shape)
    tests/                  # conftest for package bootstrap in pytest
    README.md               # this file
```

- **`ArrClient`** is a thin `httpx.AsyncClient` wrapper with the base
  URL pinned to `{url}/api/v3` and the `X-Api-Key` header injected on
  every request. It's `available` when both URL and key are set and
  raises via `response.raise_for_status()` so services can catch
  `httpx.HTTPStatusError` for clean error messages.
- **`RadarrService` / `SonarrService`** each implement four protocols:
  - `Service` — lifecycle (`start` / `stop`).
  - `ToolProvider` — declares tool definitions, routes `execute_tool`.
  - `Configurable` — declares `config_params` + `on_config_changed`.
  - `ConfigActionProvider` — declares `config_actions` + handles
    `invoke_config_action` (currently only `test_connection`).
- Each service declares a `slash_namespace` class attribute (`"radarr"`
  or `"sonarr"`), which Gilbert's AI service uses when registering
  slash commands. Tools carry `slash_command="search"`, `"find"`, etc;
  the final user-visible form is `/<namespace>.<command>`.
- `default_quality_profile` and `default_root_folder` are cached on
  the service as `_profile_choices: tuple[str, ...]` and
  `_root_folder_choices: tuple[str, ...]`. `_refresh_choices()` fetches
  them from the live server and handles partial failure gracefully
  (one list succeeds, the other can fail independently).
- Name-or-id resolution is centralized in `_resolve_movie_id` /
  `_resolve_series_id`: numeric inputs pass through, strings do a
  case-insensitive partial match against the live library. The
  quality-profile resolver does the same for profiles by id or
  partial-name match.
- The plugin reads config via the `ConfigurationReader` capability
  protocol, never from `context.config` — that only contains the
  initial plugin.yaml snapshot at load time, while the service
  namespace (`"radarr"` / `"sonarr"`) is persisted in entity storage
  and mutated by the settings UI at runtime.

## Testing

Tests live in the main Gilbert repo at `tests/unit/test_arr_plugin.py`.
The test file registers this plugin directory as a Python package at
import time (since plugins use relative imports) and skips gracefully
when the plugin isn't installed. It uses a `FakeArrClient` that records
calls and returns canned responses, so the tests have no network
dependency.

```bash
# From the Gilbert repo root
uv run pytest tests/unit/test_arr_plugin.py -v
```

60 tests cover: plugin entrypoint, service info, tool visibility /
RBAC, slash command wiring, test_connection (ok/401/403/500/network
errors), profile/root-folder cache, name-resolution fallbacks,
interactive find flow, add happy path, remove, and grab.

## Development notes

- **Adding a new tool**: append a `ToolDefinition` to the service's
  `get_tools()` with a bare `slash_command` (no group — the
  `slash_namespace` on the service already prefixes it), then add a
  `case` to `execute_tool` and implement the helper. Remember the
  `required_role` — reads are `user`, writes are `admin`.
- **Debugging API calls**: `ArrClient` raises `httpx.HTTPStatusError`
  on non-2xx responses, which the service wrapper catches in
  `execute_tool` and converts to a user-visible message. Inspect the
  original exception via Gilbert's `ai_calls.log` or the service's
  logger (`gilbert.plugins.arr.radarr_service` / `sonarr_service`).
- **Changing config params**: update `config_params()` and add a
  branch in `on_config_changed` for live-tunable fields; keep
  restart-required fields (URL, API key) gated by
  `restart_required=True` so saves hot-swap the service.
- **Plugin layering**: imports must come only from
  `gilbert.interfaces.*` and standard library / third-party packages.
  Never reach into `gilbert.core.services` or `gilbert.integrations`
  — that's an architecture violation and will show up in Gilbert's
  layer audit.

## License

Same license as the parent `briandilley/gilbert-plugins` repository.
