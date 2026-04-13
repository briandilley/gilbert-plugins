"""Radarr service — manages movies via the Radarr v3 API."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.ui import ToolOutput, UIBlock, UIElement, UIOption

from .arr_client import ArrClient

logger = logging.getLogger(__name__)


class RadarrService(Service):
    """Radarr integration — exposes movie management as AI tools + slash commands."""

    slash_namespace = "radarr"

    def __init__(self) -> None:
        self._enabled: bool = False
        self._client: ArrClient | None = None
        self._url: str = ""
        self._api_key: str = ""
        self._default_quality_profile: str = ""
        self._default_root_folder: str = ""
        # Choices cached from the live Radarr server. Populated on
        # start() and refreshed by the ``test_connection`` config action.
        # Used to render the default_quality_profile / default_root_folder
        # fields as dropdowns in the Settings UI.
        self._profile_choices: tuple[str, ...] = ()
        self._root_folder_choices: tuple[str, ...] = ()

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="radarr",
            capabilities=frozenset({"radarr", "ai_tools"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Radarr — movie library management",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe(self.config_namespace)
            if not section.get("enabled", False):
                logger.info("Radarr service disabled")
                return
            self._url = str(section.get("url", "") or "")
            self._api_key = str(section.get("api_key", "") or "")
            self._default_quality_profile = str(
                section.get("default_quality_profile", "") or ""
            )
            self._default_root_folder = str(
                section.get("default_root_folder", "") or ""
            )

        if not self._url or not self._api_key:
            logger.warning(
                "Radarr enabled but url/api_key not configured — service inactive",
            )
            return

        self._client = ArrClient("radarr", self._url, self._api_key)
        self._enabled = True
        await self._refresh_choices()
        logger.info("Radarr service started (url=%s)", self._url)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._profile_choices = ()
        self._root_folder_choices = ()
        self._enabled = False

    async def _refresh_choices(self) -> None:
        """Fetch quality profiles + root folders from the live Radarr server.

        Both lists populate dropdowns in the Settings UI. Called on
        service start and from the ``test_connection`` config action.
        Failures are non-fatal — the fields fall back to free-text input.
        """
        if self._client is None:
            return
        try:
            profiles = await self._client.get("/qualityprofile")
            self._profile_choices = tuple(
                str(p["name"]) for p in profiles if p.get("name")
            )
        except Exception:
            logger.debug("Failed to fetch Radarr quality profiles", exc_info=True)
            self._profile_choices = ()
        try:
            folders = await self._client.get("/rootfolder")
            self._root_folder_choices = tuple(
                str(f["path"]) for f in folders if f.get("path")
            )
        except Exception:
            logger.debug("Failed to fetch Radarr root folders", exc_info=True)
            self._root_folder_choices = ()

    # ── Configurable protocol ───────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "radarr"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Enable the Radarr integration.",
                default=False, restart_required=True,
            ),
            ConfigParam(
                key="url", type=ToolParameterType.STRING,
                description="Base URL of the Radarr server (e.g. http://radarr.local:7878).",
                default="", restart_required=True,
            ),
            ConfigParam(
                key="api_key", type=ToolParameterType.STRING,
                description="Radarr API key (Settings → General in Radarr).",
                default="", restart_required=True, sensitive=True,
            ),
            ConfigParam(
                key="default_quality_profile", type=ToolParameterType.STRING,
                description=(
                    "Default quality profile name used when adding a movie if "
                    "the caller doesn't specify one. Empty uses Radarr's first profile."
                ),
                default="",
                choices=self._profile_choices or None,
            ),
            ConfigParam(
                key="default_root_folder", type=ToolParameterType.STRING,
                description=(
                    "Override for the root folder path used when adding a movie. "
                    "Empty uses Radarr's first configured root folder."
                ),
                default="",
                choices=self._root_folder_choices or None,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        # Non-restart params we can apply live
        self._default_quality_profile = str(
            config.get("default_quality_profile", self._default_quality_profile) or ""
        )
        self._default_root_folder = str(
            config.get("default_root_folder", self._default_root_folder) or ""
        )

    # ── ConfigActionProvider protocol ───────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Verify Gilbert can reach Radarr and the API key is valid. "
                    "Also refreshes the quality-profile and root-folder dropdowns."
                ),
                required_role="admin",
            ),
        ]

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error", message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if not self._enabled or self._client is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "Radarr is disabled or not configured — enable the service "
                    "and fill in URL + API key, then save before testing."
                ),
            )
        try:
            status = await self._client.get("/system/status")
        except httpx.HTTPStatusError as exc:
            reason = (
                "API key rejected"
                if exc.response.status_code in (401, 403)
                else f"HTTP {exc.response.status_code}"
            )
            return ConfigActionResult(
                status="error", message=f"Radarr API error: {reason}",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error", message=f"Connection failed: {exc}",
            )

        # Refresh cached choices so the dropdowns populate on the next render.
        await self._refresh_choices()

        app = status.get("appName") or "Radarr"
        version = status.get("version") or "?"
        return ConfigActionResult(
            status="ok",
            message=(
                f"Connected to {app} {version} — "
                f"{len(self._profile_choices)} quality profile(s), "
                f"{len(self._root_folder_choices)} root folder(s)."
            ),
        )

    # ── ToolProvider protocol ───────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "radarr"

    def get_tools(self) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="radarr_search",
                slash_command="search",
                slash_help="Search Radarr for a movie to add: /radarr.search <query>",
                description=(
                    "Search Radarr's movie catalog for candidates to add. "
                    "Returns up to 5 results with TMDB IDs that can be passed to radarr_add."
                ),
                parameters=[
                    ToolParameter(
                        name="query", type=ToolParameterType.STRING,
                        description="Movie name to search for.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="radarr_find",
                slash_command="find",
                slash_help="Find and add a movie to Radarr: /radarr.find <query>",
                description=(
                    "Search Radarr by movie name and return an interactive picker: "
                    "up to 5 candidates with poster, year, and an 'Add to Radarr' "
                    "button for each. Clicking a button triggers radarr_add with "
                    "the TMDB id of the selected movie. Prefer this over "
                    "radarr_search when the user wants to *add* a movie by name."
                ),
                parameters=[
                    ToolParameter(
                        name="query", type=ToolParameterType.STRING,
                        description="Movie name to search for.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="radarr_list",
                slash_command="list",
                slash_help="List monitored movies in Radarr: /radarr.list",
                description="List all movies monitored by Radarr with their download status.",
                parameters=[],
                required_role="user",
            ),
            ToolDefinition(
                name="radarr_details",
                slash_command="details",
                slash_help="Show movie details: /radarr.details <name or movie_id>",
                description=(
                    "Get details for a movie already in the Radarr library. "
                    "Accepts either a name (partial match) or numeric Radarr movie id."
                ),
                parameters=[
                    ToolParameter(
                        name="movie", type=ToolParameterType.STRING,
                        description="Movie name (partial match) or Radarr movie id.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="radarr_upcoming",
                slash_command="upcoming",
                slash_help="Show upcoming Radarr movie releases: /radarr.upcoming",
                description="Show upcoming and recently released movies from Radarr's calendar.",
                parameters=[],
                required_role="user",
            ),
            ToolDefinition(
                name="radarr_queue",
                slash_command="queue",
                slash_help="Show Radarr download queue: /radarr.queue",
                description="Show the current Radarr download queue with progress.",
                parameters=[],
                required_role="user",
            ),
            ToolDefinition(
                name="radarr_recent",
                slash_command="recent",
                slash_help="Show recently downloaded movies: /radarr.recent [limit]",
                description=(
                    "Show the most recently imported movies in Radarr. "
                    "Pass limit=1 for the 'last' downloaded movie."
                ),
                parameters=[
                    ToolParameter(
                        name="limit", type=ToolParameterType.INTEGER,
                        description="Number of results to return (1-10). Default 5.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="radarr_profiles",
                slash_command="profiles",
                slash_help="List Radarr quality profiles: /radarr.profiles",
                description="List the quality profiles configured in Radarr.",
                parameters=[],
                required_role="user",
            ),
            ToolDefinition(
                name="radarr_add",
                slash_command="add",
                slash_help="Add a movie to Radarr: /radarr.add <tmdb_id> [quality_profile]",
                description=(
                    "Add a movie to Radarr and start searching for a download. "
                    "Requires the TMDB id from radarr_search results."
                ),
                parameters=[
                    ToolParameter(
                        name="tmdb_id", type=ToolParameterType.INTEGER,
                        description="TMDB id of the movie (from radarr_search).",
                    ),
                    ToolParameter(
                        name="quality_profile", type=ToolParameterType.STRING,
                        description=(
                            "Quality profile name or id. Partial name match. "
                            "Defaults to the configured default profile."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="monitored", type=ToolParameterType.BOOLEAN,
                        description="Whether to monitor the movie. Default true.",
                        required=False,
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="radarr_remove",
                slash_command="remove",
                slash_help="Remove a movie from Radarr: /radarr.remove <name or movie_id>",
                description=(
                    "Remove a movie from Radarr and delete its files. "
                    "Accepts a name (partial match) or numeric Radarr movie id."
                ),
                parameters=[
                    ToolParameter(
                        name="movie", type=ToolParameterType.STRING,
                        description="Movie name (partial match) or Radarr movie id.",
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="radarr_grab",
                slash_command="grab",
                slash_help="Trigger a Radarr download search: /radarr.grab <name or movie_id>",
                description=(
                    "Trigger a Radarr download search for an existing movie. "
                    "Useful when a movie is missing or you want to upgrade its release."
                ),
                parameters=[
                    ToolParameter(
                        name="movie", type=ToolParameterType.STRING,
                        description="Movie name (partial match) or Radarr movie id.",
                    ),
                ],
                required_role="admin",
            ),
        ]

    async def execute_tool(
        self, name: str, arguments: dict[str, Any],
    ) -> str | ToolOutput:
        if not self._enabled or self._client is None:
            return "Radarr is not configured."
        args = {k: v for k, v in arguments.items() if not k.startswith("_")}
        try:
            match name:
                case "radarr_search":
                    return await self._search(args.get("query"))
                case "radarr_find":
                    return await self._find(args.get("query"))
                case "radarr_list":
                    return await self._list()
                case "radarr_details":
                    return await self._details(args.get("movie"))
                case "radarr_upcoming":
                    return await self._upcoming()
                case "radarr_queue":
                    return await self._queue()
                case "radarr_recent":
                    return await self._recent(args.get("limit", 5))
                case "radarr_profiles":
                    return await self._list_profiles()
                case "radarr_add":
                    return await self._add(
                        tmdb_id=args.get("tmdb_id"),
                        quality_profile=args.get("quality_profile"),
                        monitored=args.get("monitored", True),
                    )
                case "radarr_remove":
                    return await self._remove(args.get("movie"))
                case "radarr_grab":
                    return await self._grab(args.get("movie"))
                case _:
                    raise KeyError(f"Unknown tool: {name}")
        except Exception as exc:
            logger.exception("radarr tool error: %s", name)
            return f"Sorry, I had trouble with Radarr: {exc}"

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _get_poster(item: dict[str, Any]) -> str | None:
        for img in item.get("images", []):
            if img.get("coverType") == "poster" and img.get("remoteUrl"):
                return str(img["remoteUrl"])
        return None

    async def _resolve_movie_id(self, movie: str | int | None) -> int | None:
        """Resolve a caller-supplied movie argument to a Radarr id.

        Accepts either a numeric id (as int or string) or a name (partial
        match against the library). Returns ``None`` if nothing matches.
        """
        if movie is None or movie == "":
            return None
        assert self._client is not None
        try:
            return int(movie)
        except (TypeError, ValueError):
            pass
        query = str(movie).lower()
        movies = await self._client.get("/movie")
        match = next(
            (m for m in movies if m["title"].lower() == query), None,
        ) or next(
            (m for m in movies if query in m["title"].lower()), None,
        )
        return int(match["id"]) if match else None

    # ── Action implementations ──────────────────────────────────────

    async def _search(self, query: str | None) -> str:
        if not query:
            return "Please specify a movie to search for."
        assert self._client is not None
        results = await self._client.get("/movie/lookup", params={"term": query})
        if not results:
            return f"No movies found matching '{query}'."
        lines: list[str] = []
        for m in results[:5]:
            year = m.get("year", "?")
            runtime = m.get("runtime", 0)
            tmdb_id = m.get("tmdbId")
            in_library = m.get("id", 0) > 0
            status = "in library" if in_library else "not in library"
            poster = self._get_poster(m)
            poster_md = f"![{m['title']}]({poster})\n" if poster else ""
            lines.append(
                f"- {poster_md}**{m['title']}** ({year}) — {runtime}min, "
                f"{status} (tmdb_id: {tmdb_id})"
            )
            if m.get("overview"):
                lines.append(f"  {m['overview'][:150]}")
        return "Search results:\n" + "\n".join(lines)

    async def _find(self, query: str | None) -> str | ToolOutput:
        """Interactive add flow: search by name, return a clickable picker.

        Renders up to 5 candidates as one UIBlock each. Each block has a
        single ``Add to Radarr`` button whose value is the candidate's
        TMDB id and whose element name is ``tmdb_id``. When the user
        clicks, the form submission surfaces to the AI as
        ``[user submitted: Add <title> (<year>) to Radarr?]`` with
        ``tmdb_id: <n>`` — unambiguous enough for the AI to call
        ``radarr_add`` with the right id.
        """
        if not query:
            return "Please specify a movie to search for."
        assert self._client is not None
        results = await self._client.get("/movie/lookup", params={"term": query})
        if not results:
            return f"No movies found matching '{query}'."

        blocks: list[UIBlock] = []
        text_lines: list[str] = [
            f"Found {min(5, len(results))} match(es) for '{query}' — "
            f"click **Add to Radarr** on the one you want:",
        ]

        for m in results[:5]:
            title = m.get("title") or "Unknown"
            year = m.get("year", "?")
            runtime = m.get("runtime", 0)
            tmdb_id = m.get("tmdbId")
            in_library = m.get("id", 0) > 0
            overview = (m.get("overview") or "").strip()
            poster = self._get_poster(m)

            if in_library:
                text_lines.append(
                    f"- **{title}** ({year}) — already in your library"
                )
            else:
                text_lines.append(f"- **{title}** ({year}) — {runtime}min")

            meta_lines = [f"{title} ({year})", f"{runtime} min"]
            if in_library:
                meta_lines.append("Already in your library.")
            else:
                meta_lines.append(f"TMDB id: {tmdb_id}")
            if overview:
                snippet = overview[:300] + ("…" if len(overview) > 300 else "")
                meta_lines.append("")
                meta_lines.append(snippet)
            label_text = "\n".join(meta_lines)

            elements: list[UIElement] = []
            if poster:
                elements.append(UIElement(
                    type="image", name="poster",
                    url=poster, label=title, max_width=96,
                ))
            elements.append(
                UIElement(type="label", name="info", label=label_text),
            )
            if not in_library and tmdb_id is not None:
                elements.append(
                    UIElement(
                        type="buttons",
                        name="tmdb_id",
                        options=[
                            UIOption(
                                value=str(tmdb_id),
                                label="Add to Radarr",
                            ),
                        ],
                    ),
                )

            blocks.append(UIBlock(
                title=f"Add {title} ({year}) to Radarr?",
                elements=elements,
                submit_label="Add to Radarr",
            ))

        return ToolOutput(text="\n".join(text_lines), ui_blocks=blocks)

    async def _list(self) -> str:
        assert self._client is not None
        movies = await self._client.get("/movie")
        if not movies:
            return "No movies in Radarr."
        monitored = [m for m in movies if m.get("monitored")]
        lines: list[str] = []
        for m in sorted(monitored, key=lambda x: x["title"])[:30]:
            year = m.get("year", "?")
            has_file = m.get("hasFile", False)
            status = "downloaded" if has_file else "missing"
            lines.append(
                f"- **{m['title']}** ({year}) — {status} [id: {m['id']}]"
            )
        total = len(monitored)
        shown = min(30, total)
        header = (
            f"Monitored movies ({shown}/{total}):\n"
            if total > 30
            else f"Monitored movies ({total}):\n"
        )
        return header + "\n".join(lines)

    async def _details(self, movie: str | int | None) -> str:
        movie_id = await self._resolve_movie_id(movie)
        if not movie_id:
            return (
                f"Movie '{movie}' not found in library. "
                f"Try searching first with radarr_search."
            )
        assert self._client is not None
        m = await self._client.get(f"/movie/{movie_id}")
        has_file = m.get("hasFile", False)
        file_info = ""
        if has_file and m.get("movieFile"):
            mf = m["movieFile"]
            quality = mf.get("quality", {}).get("quality", {}).get("name", "?")
            size_gb = mf.get("size", 0) / 1_073_741_824
            file_info = f"\nFile: {quality}, {size_gb:.1f} GB"
        poster = self._get_poster(m)
        poster_md = f"![{m['title']}]({poster}) " if poster else ""
        overview = m.get("overview", "")
        overview_line = f"\n{overview[:200]}" if overview else ""
        return (
            f"{poster_md}**{m['title']}** ({m.get('year', '?')})\n"
            f"Status: {'downloaded' if has_file else 'missing'}\n"
            f"Runtime: {m.get('runtime', '?')} min\n"
            f"Genres: {', '.join(m.get('genres', []))}\n"
            f"Monitored: {m.get('monitored', False)}\n"
            f"ID: {m['id']}"
            f"{file_info}"
            f"{overview_line}"
        )

    async def _upcoming(self) -> str:
        assert self._client is not None
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        end = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
        calendar = await self._client.get(
            "/calendar", params={"start": start, "end": end},
        )
        if not calendar:
            return "No upcoming movie releases."
        lines: list[str] = []
        for m in calendar[:15]:
            has_file = m.get("hasFile", False)
            status = "downloaded" if has_file else "upcoming"
            release = ""
            if m.get("digitalRelease"):
                release = f"digital: {m['digitalRelease'][:10]}"
            elif m.get("physicalRelease"):
                release = f"physical: {m['physicalRelease'][:10]}"
            elif m.get("inCinemas"):
                release = f"cinema: {m['inCinemas'][:10]}"
            lines.append(
                f"- **{m['title']}** ({m.get('year', '?')}) — {release} ({status})"
            )
        return "Upcoming movies:\n" + "\n".join(lines)

    async def _queue(self) -> str:
        assert self._client is not None
        data = await self._client.get("/queue", params={"pageSize": 20})
        records = data.get("records", [])
        if not records:
            return "Download queue is empty."
        movie_cache: dict[int, dict[str, Any]] = {}
        lines: list[str] = []
        for item in records:
            mid = item.get("movieId")
            if mid and mid not in movie_cache:
                try:
                    movie_cache[mid] = await self._client.get(f"/movie/{mid}")
                except Exception:
                    movie_cache[mid] = {}
            movie = movie_cache.get(mid, {})
            title = movie.get("title", item.get("title", "Unknown"))
            status = item.get("status", "?")
            state = item.get("trackedDownloadState", "")
            pct = item.get("sizeleft", 0)
            size = item.get("size", 1)
            progress = ((size - pct) / size * 100) if size else 0
            display_status = state if state else status
            lines.append(f"- **{title}** — {display_status} ({progress:.0f}%)")
        return "Download queue:\n" + "\n".join(lines)

    async def _recent(self, limit: int | None) -> str:
        assert self._client is not None
        n = min(max(1, int(limit or 5)), 10)
        data = await self._client.get(
            "/history",
            params={
                "pageSize": 30,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        records = [
            r for r in data.get("records", [])
            if r.get("eventType") == "downloadFolderImported"
        ][:n]
        if not records:
            return "No recent movie downloads."

        movie_cache: dict[int, dict[str, Any]] = {}
        for r in records:
            mid = r.get("movieId")
            if mid and mid not in movie_cache:
                try:
                    movie_cache[mid] = await self._client.get(f"/movie/{mid}")
                except Exception:
                    movie_cache[mid] = {}

        lines: list[str] = []
        for r in records:
            movie = movie_cache.get(r.get("movieId"), {})
            title = movie.get("title", r.get("sourceTitle", "Unknown"))
            year = movie.get("year", "?")
            date = r.get("date", "?")[:16].replace("T", " ")
            quality = (
                r.get("quality", {}).get("quality", {}).get("name", "?")
            )
            poster = self._get_poster(movie)
            poster_md = f"![{title}]({poster}) " if poster else ""
            lines.append(
                f"- {poster_md}**{title}** ({year}) — {quality}, {date}"
            )
        return "Recently downloaded movies:\n" + "\n".join(lines)

    async def _list_profiles(self) -> str:
        assert self._client is not None
        profiles = await self._client.get("/qualityprofile")
        if not profiles:
            return "No quality profiles found."
        lines = [f"- {p['name']} (id: {p['id']})" for p in profiles]
        return "Quality profiles:\n" + "\n".join(lines)

    async def _add(
        self,
        tmdb_id: Any,
        quality_profile: Any,
        monitored: Any,
    ) -> str:
        if not tmdb_id:
            return "Please provide a tmdb_id (get one from radarr_search)."
        try:
            tmdb_id_int = int(tmdb_id)
        except (TypeError, ValueError):
            return f"Invalid tmdb_id: {tmdb_id}"
        assert self._client is not None

        results = await self._client.get(
            "/movie/lookup", params={"term": f"tmdb:{tmdb_id_int}"},
        )
        if not results:
            return "Movie not found on TMDB."
        movie = results[0]

        if movie.get("id", 0) > 0:
            return f"**{movie['title']}** is already in your library."

        root_folders = await self._client.get("/rootfolder")
        if not root_folders:
            return "No root folders configured in Radarr."
        root_path = self._default_root_folder or root_folders[0]["path"]

        profiles = await self._client.get("/qualityprofile")
        if not profiles:
            return "No quality profiles found."
        profile_input = quality_profile or self._default_quality_profile or None
        profile_id = self._resolve_profile_id(profiles, profile_input)
        if profile_id is None:
            names = ", ".join(str(p["name"]) for p in profiles)
            return f"Quality profile '{profile_input}' not found. Available: {names}"

        movie["qualityProfileId"] = profile_id
        movie["rootFolderPath"] = root_path
        movie["monitored"] = bool(monitored) if monitored is not None else True
        movie["addOptions"] = {"searchForMovie": True}

        result = await self._client.post("/movie", data=movie)
        return (
            f"Added **{result['title']}** to Radarr. Searching for a download."
        )

    @staticmethod
    def _resolve_profile_id(
        profiles: list[dict[str, Any]], profile_input: Any,
    ) -> int | None:
        """Resolve a user-supplied profile identifier to a numeric id.

        Accepts ids (int or numeric string) and names (case-insensitive
        partial match). Returns the first profile's id as a fallback when
        nothing was specified.
        """
        if profile_input in (None, "", 0):
            return int(profiles[0]["id"]) if profiles else None
        try:
            pid = int(profile_input)
            if any(p["id"] == pid for p in profiles):
                return pid
        except (TypeError, ValueError):
            pass
        needle = str(profile_input).lower()
        match = next(
            (p for p in profiles if needle in str(p["name"]).lower()), None,
        )
        return int(match["id"]) if match else None

    async def _remove(self, movie: str | int | None) -> str:
        movie_id = await self._resolve_movie_id(movie)
        if not movie_id:
            return f"Movie '{movie}' not found."
        assert self._client is not None
        await self._client.delete(
            f"/movie/{movie_id}", params={"deleteFiles": "true"},
        )
        return f"Removed movie {movie_id} and deleted files."

    async def _grab(self, movie: str | int | None) -> str:
        movie_id = await self._resolve_movie_id(movie)
        if not movie_id:
            return f"Movie '{movie}' not found."
        assert self._client is not None
        await self._client.post(
            "/command", data={"name": "MoviesSearch", "movieIds": [movie_id]},
        )
        return f"Triggered download search for movie {movie_id}."
