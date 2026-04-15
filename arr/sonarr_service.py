"""Sonarr service — manages TV shows via the Sonarr v3 API."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from gilbert.interfaces.auth import UserContext
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


class SonarrService(Service):
    """Sonarr integration — exposes TV show management as AI tools + slash commands."""

    slash_namespace = "sonarr"

    def __init__(self) -> None:
        self._enabled: bool = False
        self._client: ArrClient | None = None
        self._url: str = ""
        self._api_key: str = ""
        self._default_quality_profile: str = ""
        self._default_root_folder: str = ""
        # Choices cached from the live Sonarr server. Populated on
        # start() and refreshed by the ``test_connection`` config action.
        self._profile_choices: tuple[str, ...] = ()
        self._root_folder_choices: tuple[str, ...] = ()

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="sonarr",
            capabilities=frozenset({"sonarr", "ai_tools"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Sonarr — TV show library management",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe(self.config_namespace)
            if not section.get("enabled", False):
                logger.info("Sonarr service disabled")
                return
            self._url = str(section.get("url", "") or "")
            self._api_key = str(section.get("api_key", "") or "")
            self._default_quality_profile = str(section.get("default_quality_profile", "") or "")
            self._default_root_folder = str(section.get("default_root_folder", "") or "")

        if not self._url or not self._api_key:
            logger.warning(
                "Sonarr enabled but url/api_key not configured — service inactive",
            )
            return

        self._client = ArrClient("sonarr", self._url, self._api_key)
        self._enabled = True
        await self._refresh_choices()
        logger.info("Sonarr service started (url=%s)", self._url)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._profile_choices = ()
        self._root_folder_choices = ()
        self._enabled = False

    async def _refresh_choices(self) -> None:
        """Fetch quality profiles + root folders from the live Sonarr server.

        Both lists populate dropdowns in the Settings UI. Called on
        service start and from the ``test_connection`` config action.
        Failures are non-fatal — the fields fall back to free-text input.
        """
        if self._client is None:
            return
        try:
            profiles = await self._client.get("/qualityprofile")
            self._profile_choices = tuple(str(p["name"]) for p in profiles if p.get("name"))
        except Exception:
            logger.debug("Failed to fetch Sonarr quality profiles", exc_info=True)
            self._profile_choices = ()
        try:
            folders = await self._client.get("/rootfolder")
            self._root_folder_choices = tuple(str(f["path"]) for f in folders if f.get("path"))
        except Exception:
            logger.debug("Failed to fetch Sonarr root folders", exc_info=True)
            self._root_folder_choices = ()

    # ── Configurable protocol ───────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "sonarr"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Enable the Sonarr integration.",
                default=False,
                restart_required=True,
            ),
            ConfigParam(
                key="url",
                type=ToolParameterType.STRING,
                description="Base URL of the Sonarr server (e.g. http://sonarr.local:8989).",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="Sonarr API key (Settings → General in Sonarr).",
                default="",
                restart_required=True,
                sensitive=True,
            ),
            ConfigParam(
                key="default_quality_profile",
                type=ToolParameterType.STRING,
                description=(
                    "Default quality profile name used when adding a show if "
                    "the caller doesn't specify one. Empty uses Sonarr's first profile."
                ),
                default="",
                choices=self._profile_choices or None,
            ),
            ConfigParam(
                key="default_root_folder",
                type=ToolParameterType.STRING,
                description=(
                    "Override for the root folder path used when adding a show. "
                    "Empty uses Sonarr's first configured root folder."
                ),
                default="",
                choices=self._root_folder_choices or None,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
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
                    "Verify Gilbert can reach Sonarr and the API key is valid. "
                    "Also refreshes the quality-profile and root-folder dropdowns."
                ),
                required_role="admin",
            ),
        ]

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if not self._enabled or self._client is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "Sonarr is disabled or not configured — enable the service "
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
                status="error",
                message=f"Sonarr API error: {reason}",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )

        await self._refresh_choices()

        app = status.get("appName") or "Sonarr"
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
        return "sonarr"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="sonarr_search",
                slash_command="search",
                slash_help="Search Sonarr for a show to add: /sonarr.search <query>",
                description=(
                    "Search Sonarr's show catalog for candidates to add. "
                    "Returns up to 5 results with TVDB IDs that can be passed to sonarr_add."
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Show name to search for.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="sonarr_find",
                slash_command="find",
                slash_help="Find and add a show to Sonarr: /sonarr.find <query>",
                description=(
                    "Search Sonarr by show name and return an interactive picker: "
                    "up to 5 candidates with poster, year, and an 'Add to Sonarr' "
                    "button for each. Clicking a button triggers sonarr_add with "
                    "the TVDB id of the selected show. Prefer this over "
                    "sonarr_search when the user wants to *add* a show by name."
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Show name to search for.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="sonarr_list",
                slash_command="list",
                slash_help="List monitored shows in Sonarr: /sonarr.list",
                description="List all shows monitored by Sonarr with their download progress.",
                parameters=[],
                required_role="user",
            ),
            ToolDefinition(
                name="sonarr_details",
                slash_command="details",
                slash_help="Show details for a show: /sonarr.details <name or series_id>",
                description=(
                    "Get details for a show already in the Sonarr library. "
                    "Accepts a name (partial match) or numeric Sonarr series id."
                ),
                parameters=[
                    ToolParameter(
                        name="show",
                        type=ToolParameterType.STRING,
                        description="Show name (partial match) or Sonarr series id.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="sonarr_episodes",
                slash_command="episodes",
                slash_help="Show episode info for a show: /sonarr.episodes <name or series_id>",
                description=(
                    "Show episode stats and recent episodes for a monitored show. "
                    "Indicates latest aired, latest downloaded, and counts."
                ),
                parameters=[
                    ToolParameter(
                        name="show",
                        type=ToolParameterType.STRING,
                        description="Show name (partial match) or Sonarr series id.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="sonarr_upcoming",
                slash_command="upcoming",
                slash_help="Show upcoming Sonarr episodes: /sonarr.upcoming",
                description="Show upcoming episodes airing in the next 2 weeks.",
                parameters=[],
                required_role="user",
            ),
            ToolDefinition(
                name="sonarr_queue",
                slash_command="queue",
                slash_help="Show Sonarr download queue: /sonarr.queue",
                description="Show the current Sonarr download queue with progress.",
                parameters=[],
                required_role="user",
            ),
            ToolDefinition(
                name="sonarr_recent",
                slash_command="recent",
                slash_help="Show recently downloaded episodes: /sonarr.recent [limit]",
                description=(
                    "Show the most recently imported episodes in Sonarr. "
                    "Pass limit=1 for the 'last' downloaded episode."
                ),
                parameters=[
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Number of results to return (1-10). Default 5.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="sonarr_profiles",
                slash_command="profiles",
                slash_help="List Sonarr quality profiles: /sonarr.profiles",
                description="List the quality profiles configured in Sonarr.",
                parameters=[],
                required_role="user",
            ),
            ToolDefinition(
                name="sonarr_add",
                slash_command="add",
                slash_help="Add a show to Sonarr: /sonarr.add <tvdb_id> [quality_profile]",
                description=(
                    "Add a show to Sonarr and start searching for episodes. "
                    "Requires the TVDB id from sonarr_search results."
                ),
                parameters=[
                    ToolParameter(
                        name="tvdb_id",
                        type=ToolParameterType.INTEGER,
                        description="TVDB id of the show (from sonarr_search).",
                    ),
                    ToolParameter(
                        name="quality_profile",
                        type=ToolParameterType.STRING,
                        description=(
                            "Quality profile name or id. Partial name match. "
                            "Defaults to the configured default profile."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="monitored",
                        type=ToolParameterType.BOOLEAN,
                        description="Whether to monitor new episodes. Default true.",
                        required=False,
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="sonarr_remove",
                slash_command="remove",
                slash_help="Remove a show from Sonarr: /sonarr.remove <name or series_id>",
                description=(
                    "Remove a show from Sonarr and delete its files. "
                    "Accepts a name (partial match) or numeric Sonarr series id."
                ),
                parameters=[
                    ToolParameter(
                        name="show",
                        type=ToolParameterType.STRING,
                        description="Show name (partial match) or Sonarr series id.",
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="sonarr_grab",
                slash_command="grab",
                slash_help="Search for missing episodes: /sonarr.grab <name or series_id>",
                description=("Trigger a Sonarr search for missing episodes of a show."),
                parameters=[
                    ToolParameter(
                        name="show",
                        type=ToolParameterType.STRING,
                        description="Show name (partial match) or Sonarr series id.",
                    ),
                ],
                required_role="admin",
            ),
        ]

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str | ToolOutput:
        if not self._enabled or self._client is None:
            return "Sonarr is not configured."
        args = {k: v for k, v in arguments.items() if not k.startswith("_")}
        try:
            match name:
                case "sonarr_search":
                    return await self._search(args.get("query"))
                case "sonarr_find":
                    return await self._find(args.get("query"))
                case "sonarr_list":
                    return await self._list()
                case "sonarr_details":
                    return await self._details(args.get("show"))
                case "sonarr_episodes":
                    return await self._episodes(args.get("show"))
                case "sonarr_upcoming":
                    return await self._upcoming()
                case "sonarr_queue":
                    return await self._queue()
                case "sonarr_recent":
                    return await self._recent(args.get("limit", 5))
                case "sonarr_profiles":
                    return await self._list_profiles()
                case "sonarr_add":
                    return await self._add(
                        tvdb_id=args.get("tvdb_id"),
                        quality_profile=args.get("quality_profile"),
                        monitored=args.get("monitored", True),
                    )
                case "sonarr_remove":
                    return await self._remove(args.get("show"))
                case "sonarr_grab":
                    return await self._grab(args.get("show"))
                case _:
                    raise KeyError(f"Unknown tool: {name}")
        except Exception as exc:
            logger.exception("sonarr tool error: %s", name)
            return f"Sorry, I had trouble with Sonarr: {exc}"

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _get_poster(item: dict[str, Any]) -> str | None:
        for img in item.get("images", []):
            if img.get("coverType") == "poster" and img.get("remoteUrl"):
                return str(img["remoteUrl"])
        return None

    async def _resolve_series_id(self, show: str | int | None) -> int | None:
        """Resolve a caller-supplied show argument to a Sonarr series id.

        Accepts a numeric id or a name (partial match against the library).
        """
        if show is None or show == "":
            return None
        assert self._client is not None
        try:
            return int(show)
        except (TypeError, ValueError):
            pass
        query = str(show).lower()
        series = await self._client.get("/series")
        match = next(
            (s for s in series if s["title"].lower() == query),
            None,
        ) or next(
            (s for s in series if query in s["title"].lower()),
            None,
        )
        return int(match["id"]) if match else None

    @staticmethod
    def _resolve_profile_id(
        profiles: list[dict[str, Any]],
        profile_input: Any,
    ) -> int | None:
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
            (p for p in profiles if needle in str(p["name"]).lower()),
            None,
        )
        return int(match["id"]) if match else None

    # ── Action implementations ──────────────────────────────────────

    async def _search(self, query: str | None) -> str:
        if not query:
            return "Please specify a show to search for."
        assert self._client is not None
        results = await self._client.get("/series/lookup", params={"term": query})
        if not results:
            return f"No shows found matching '{query}'."
        lines: list[str] = []
        for s in results[:5]:
            year = s.get("year", "?")
            seasons = s.get("seasonCount", "?")
            status = s.get("status", "unknown")
            tvdb_id = s.get("tvdbId")
            poster = self._get_poster(s)
            poster_md = f"![{s['title']}]({poster})\n" if poster else ""
            lines.append(
                f"- {poster_md}**{s['title']}** ({year}) — {seasons} seasons, "
                f"{status} (tvdb_id: {tvdb_id})"
            )
            if s.get("overview"):
                lines.append(f"  {s['overview'][:150]}")
        return "Search results:\n" + "\n".join(lines)

    async def _find(self, query: str | None) -> str | ToolOutput:
        """Interactive add flow: search by name, return a clickable picker.

        See ``RadarrService._find`` for the full design rationale.
        """
        if not query:
            return "Please specify a show to search for."
        assert self._client is not None
        results = await self._client.get("/series/lookup", params={"term": query})
        if not results:
            return f"No shows found matching '{query}'."

        blocks: list[UIBlock] = []
        text_lines: list[str] = [
            f"Found {min(5, len(results))} match(es) for '{query}' — "
            f"click **Add to Sonarr** on the one you want:",
        ]

        for s in results[:5]:
            title = s.get("title") or "Unknown"
            year = s.get("year", "?")
            seasons = s.get("seasonCount", "?")
            status = s.get("status", "unknown")
            tvdb_id = s.get("tvdbId")
            in_library = s.get("id", 0) > 0
            overview = (s.get("overview") or "").strip()
            poster = self._get_poster(s)

            if in_library:
                text_lines.append(f"- **{title}** ({year}) — already in your library")
            else:
                text_lines.append(f"- **{title}** ({year}) — {seasons} seasons, {status}")

            meta_lines = [
                f"{title} ({year})",
                f"{seasons} seasons · {status}",
            ]
            if in_library:
                meta_lines.append("Already in your library.")
            else:
                meta_lines.append(f"TVDB id: {tvdb_id}")
            if overview:
                snippet = overview[:300] + ("…" if len(overview) > 300 else "")
                meta_lines.append("")
                meta_lines.append(snippet)
            label_text = "\n".join(meta_lines)

            elements: list[UIElement] = []
            if poster:
                elements.append(
                    UIElement(
                        type="image",
                        name="poster",
                        url=poster,
                        label=title,
                        max_width=96,
                    )
                )
            elements.append(
                UIElement(type="label", name="info", label=label_text),
            )
            if not in_library and tvdb_id is not None:
                elements.append(
                    UIElement(
                        type="buttons",
                        name="tvdb_id",
                        options=[
                            UIOption(
                                value=str(tvdb_id),
                                label="Add to Sonarr",
                            ),
                        ],
                    ),
                )

            blocks.append(
                UIBlock(
                    title=f"Add {title} ({year}) to Sonarr?",
                    elements=elements,
                    submit_label="Add to Sonarr",
                )
            )

        return ToolOutput(text="\n".join(text_lines), ui_blocks=blocks)

    async def _list(self) -> str:
        assert self._client is not None
        series = await self._client.get("/series")
        if not series:
            return "No shows in Sonarr."
        monitored = [s for s in series if s.get("monitored")]
        lines: list[str] = []
        for s in sorted(monitored, key=lambda x: x["title"]):
            eps = s.get("statistics", {})
            have = eps.get("episodeFileCount", 0)
            total = eps.get("totalEpisodeCount", 0)
            pct = eps.get("percentOfEpisodes", 0)
            lines.append(
                f"- **{s['title']}** — {have}/{total} episodes ({pct:.0f}%) [id: {s['id']}]"
            )
        return f"Monitored shows ({len(monitored)}):\n" + "\n".join(lines)

    async def _details(self, show: str | int | None) -> str:
        series_id = await self._resolve_series_id(show)
        if not series_id:
            return f"Show '{show}' not found in library."
        assert self._client is not None
        s = await self._client.get(f"/series/{series_id}")
        eps = s.get("statistics", {})
        poster = self._get_poster(s)
        poster_md = f"![{s['title']}]({poster}) " if poster else ""
        overview = s.get("overview", "")
        overview_line = f"\n{overview[:200]}" if overview else ""
        return (
            f"{poster_md}**{s['title']}** ({s.get('year', '?')})\n"
            f"Status: {s.get('status', '?')}\n"
            f"Seasons: {s.get('seasonCount', '?')}\n"
            f"Episodes: {eps.get('episodeFileCount', 0)}/{eps.get('totalEpisodeCount', 0)}\n"
            f"Network: {s.get('network', '?')}\n"
            f"Monitored: {s.get('monitored', False)}\n"
            f"ID: {s['id']}"
            f"{overview_line}"
        )

    async def _episodes(self, show: str | int | None) -> str:
        series_id = await self._resolve_series_id(show)
        if not series_id:
            return f"Show '{show}' not found in library."
        assert self._client is not None
        s = await self._client.get(f"/series/{series_id}")
        episodes = await self._client.get(
            "/episode",
            params={"seriesId": series_id},
        )
        if not episodes:
            return f"No episodes found for {s['title']}."

        episodes.sort(
            key=lambda e: (e.get("seasonNumber", 0), e.get("episodeNumber", 0)),
            reverse=True,
        )

        real = [e for e in episodes if e.get("seasonNumber", 0) > 0]
        latest_aired = next((e for e in real if e.get("airDate")), None)
        latest_downloaded = next((e for e in real if e.get("hasFile")), None)

        lines: list[str] = [f"**{s['title']}**"]
        if latest_aired:
            sn = latest_aired["seasonNumber"]
            ep = latest_aired["episodeNumber"]
            title = latest_aired.get("title", "")
            has = latest_aired.get("hasFile", False)
            status = "downloaded" if has else "missing"
            lines.append(
                f'Latest aired: S{sn:02d}E{ep:02d} "{title}" — '
                f"{latest_aired.get('airDate', '?')} ({status})"
            )
        if latest_downloaded and latest_downloaded is not latest_aired:
            sn = latest_downloaded["seasonNumber"]
            ep = latest_downloaded["episodeNumber"]
            title = latest_downloaded.get("title", "")
            lines.append(
                f'Latest downloaded: S{sn:02d}E{ep:02d} "{title}" — '
                f"{latest_downloaded.get('airDate', '?')}"
            )

        total = len(real)
        have = len([e for e in real if e.get("hasFile")])
        missing = total - have
        lines.append(f"Episodes: {have}/{total} downloaded, {missing} missing")

        recent = real[:5]
        if recent:
            lines.append("\nRecent episodes:")
            for e in recent:
                sn = e["seasonNumber"]
                ep = e["episodeNumber"]
                title = e.get("title", "")
                has = "downloaded" if e.get("hasFile") else "missing"
                lines.append(f'  S{sn:02d}E{ep:02d} "{title}" — {e.get("airDate", "?")} ({has})')

        return "\n".join(lines)

    async def _upcoming(self) -> str:
        assert self._client is not None
        start = datetime.now().strftime("%Y-%m-%d")
        end = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
        calendar = await self._client.get(
            "/calendar",
            params={"start": start, "end": end},
        )
        if not calendar:
            return "No upcoming episodes in the next 2 weeks."
        lines: list[str] = []
        for ep in calendar[:15]:
            show = ep.get("series", {}).get("title", "Unknown")
            sn = ep.get("seasonNumber", 0)
            en = ep.get("episodeNumber", 0)
            title = ep.get("title", "")
            air = ep.get("airDate", "?")
            has_file = ep.get("hasFile", False)
            status = "downloaded" if has_file else "upcoming"
            lines.append(f'- **{show}** S{sn:02d}E{en:02d} "{title}" — {air} ({status})')
        return "Upcoming episodes:\n" + "\n".join(lines)

    async def _queue(self) -> str:
        assert self._client is not None
        data = await self._client.get("/queue", params={"pageSize": 20})
        records = data.get("records", [])
        if not records:
            return "Download queue is empty."
        series_cache: dict[int, dict[str, Any]] = {}
        lines: list[str] = []
        for item in records:
            sid = item.get("seriesId")
            if sid and sid not in series_cache:
                try:
                    series_cache[sid] = await self._client.get(f"/series/{sid}")
                except Exception:
                    series_cache[sid] = {}
            show = series_cache.get(sid, {}).get(
                "title",
                item.get("title", "Unknown"),
            )
            sn = item.get("seasonNumber", 0)
            status = item.get("status", "?")
            state = item.get("trackedDownloadState", "")
            pct = item.get("sizeleft", 0)
            size = item.get("size", 1)
            progress = ((size - pct) / size * 100) if size else 0
            display_status = state if state else status
            lines.append(f"- **{show}** S{sn:02d} — {display_status} ({progress:.0f}%)")
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
            r for r in data.get("records", []) if r.get("eventType") == "downloadFolderImported"
        ][:n]
        if not records:
            return "No recent downloads."

        series_cache: dict[int, dict[str, Any]] = {}
        episode_cache: dict[int, dict[str, Any]] = {}
        for r in records:
            sid = r.get("seriesId")
            eid = r.get("episodeId")
            if sid and sid not in series_cache:
                try:
                    series_cache[sid] = await self._client.get(f"/series/{sid}")
                except Exception:
                    series_cache[sid] = {}
            if eid and eid not in episode_cache:
                try:
                    episode_cache[eid] = await self._client.get(f"/episode/{eid}")
                except Exception:
                    episode_cache[eid] = {}

        lines: list[str] = []
        for r in records:
            series = series_cache.get(r.get("seriesId"), {})
            episode = episode_cache.get(r.get("episodeId"), {})
            show = series.get("title", "Unknown")
            sn = episode.get("seasonNumber", 0)
            en = episode.get("episodeNumber", 0)
            title = episode.get("title", "")
            date = r.get("date", "?")[:16].replace("T", " ")
            quality = r.get("quality", {}).get("quality", {}).get("name", "?")
            poster = self._get_poster(series)
            poster_md = f"![{show}]({poster}) " if poster else ""
            lines.append(
                f'- {poster_md}**{show}** S{sn:02d}E{en:02d} "{title}" — {quality}, {date}'
            )
        return "Recently downloaded episodes:\n" + "\n".join(lines)

    async def _list_profiles(self) -> str:
        assert self._client is not None
        profiles = await self._client.get("/qualityprofile")
        if not profiles:
            return "No quality profiles found."
        lines = [f"- {p['name']} (id: {p['id']})" for p in profiles]
        return "Quality profiles:\n" + "\n".join(lines)

    async def _add(
        self,
        tvdb_id: Any,
        quality_profile: Any,
        monitored: Any,
    ) -> str:
        if not tvdb_id:
            return "Please provide a tvdb_id (get one from sonarr_search)."
        try:
            tvdb_id_int = int(tvdb_id)
        except (TypeError, ValueError):
            return f"Invalid tvdb_id: {tvdb_id}"
        assert self._client is not None

        results = await self._client.get(
            "/series/lookup",
            params={"term": f"tvdb:{tvdb_id_int}"},
        )
        if not results:
            return "Show not found on TVDB."
        show = results[0]

        if show.get("id", 0) > 0:
            return f"**{show['title']}** is already in your library."

        root_folders = await self._client.get("/rootfolder")
        if not root_folders:
            return "No root folders configured in Sonarr."
        root_path = self._default_root_folder or root_folders[0]["path"]

        profiles = await self._client.get("/qualityprofile")
        if not profiles:
            return "No quality profiles found."
        profile_input = quality_profile or self._default_quality_profile or None
        profile_id = self._resolve_profile_id(profiles, profile_input)
        if profile_id is None:
            names = ", ".join(str(p["name"]) for p in profiles)
            return f"Quality profile '{profile_input}' not found. Available: {names}"

        show["qualityProfileId"] = profile_id
        show["rootFolderPath"] = root_path
        show["monitored"] = bool(monitored) if monitored is not None else True
        show["addOptions"] = {"searchForMissingEpisodes": True}

        result = await self._client.post("/series", data=show)
        return f"Added **{result['title']}** to Sonarr. Searching for episodes."

    async def _remove(self, show: str | int | None) -> str:
        series_id = await self._resolve_series_id(show)
        if not series_id:
            return f"Show '{show}' not found."
        assert self._client is not None
        await self._client.delete(
            f"/series/{series_id}",
            params={"deleteFiles": "true"},
        )
        return f"Removed series {series_id} and deleted files."

    async def _grab(self, show: str | int | None) -> str:
        series_id = await self._resolve_series_id(show)
        if not series_id:
            return f"Show '{show}' not found."
        assert self._client is not None
        await self._client.post(
            "/command",
            data={"name": "SeriesSearch", "seriesId": series_id},
        )
        return f"Triggered search for missing episodes of series {series_id}."
