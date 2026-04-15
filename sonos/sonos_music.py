"""Sonos music backend — browse, search, and play via SoCo.



Uses the Sonos system itself as the source of truth for music: favorites
and playlists come from the Sonos Music Library (no auth), and search
runs through SoCo's SMAPI client against a configured linked service
(default: Spotify).

The SMAPI client requires a one-time authentication flow to call
``search()``. The backend exposes this as two action buttons on its
settings page (``link_spotify`` and ``link_spotify_complete``) so an
admin can complete the link from the web UI without dropping to a shell.
The resulting token/key pair is persisted back into the backend's
settings via the ``ConfigActionResult.data['persist']`` side-channel.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from typing import Any

import soco
from soco import SoCo
from soco.music_services import MusicService as SonosSMAPI
from soco.music_services.token_store import TokenStoreBase

from gilbert.interfaces.configuration import ConfigAction, ConfigActionResult
from gilbert.interfaces.music import (
    MusicBackend,
    MusicItem,
    MusicItemKind,
    MusicSearchUnavailableError,
    Playable,
)

logger = logging.getLogger(__name__)

# Map SMAPI search category names to our MusicItemKind values
_KIND_TO_SMAPI: dict[MusicItemKind, str] = {
    MusicItemKind.TRACK: "tracks",
    MusicItemKind.ALBUM: "albums",
    MusicItemKind.ARTIST: "artists",
    MusicItemKind.PLAYLIST: "playlists",
    MusicItemKind.STATION: "stations",
}


class _InMemoryTokenStore(TokenStoreBase):
    """A token store that holds a single token pair in memory.

    SoCo's ``MusicService`` persists search-auth tokens via a
    ``TokenStoreBase``; the default implementation writes to a JSON file
    in the user's config dir. We substitute this in-memory store so that
    tokens live in Gilbert's own config instead, and we can introspect
    them after ``complete_authentication`` to persist them.
    """

    def __init__(self, token: str = "", key: str = "") -> None:
        super().__init__()
        self._token = token
        self._key = key

    @property
    def token(self) -> str:
        return self._token

    @property
    def key(self) -> str:
        return self._key

    def save_token_pair(
        self,
        music_service_id: Any,
        household_id: Any,
        token_pair: Any,
    ) -> None:
        self._token, self._key = token_pair[0], token_pair[1]

    def load_token_pair(
        self,
        music_service_id: Any,
        household_id: Any,
    ) -> tuple[str, str]:
        if not self._token:
            raise KeyError("No SMAPI token stored — run link flow first")
        return (self._token, self._key)

    def has_token(self, music_service_id: Any, household_id: Any) -> bool:
        return bool(self._token)


def _item_id(item: Any) -> str:
    """Best-effort extraction of an opaque item id from a SoCo didl object."""
    for attr in ("item_id", "get_id", "id"):
        val = getattr(item, attr, None)
        if callable(val):
            try:
                val = val()
            except Exception:
                val = None
        if val:
            return str(val)
    return ""


# Match ``spotify:<kind>:<id>`` anywhere in a string. SMAPI item ids for
# Spotify come back in shapes like ``0fffffffspotify:track:3w0pyHgJJW9JN0cJxmi33Z``
# (8-char hex "flags" prefix + percent- or un-encoded Spotify URI) and we
# want the clean URI out so the speaker backend can build the real
# ``x-sonos-spotify:...`` playback URI via ``_to_sonos_spotify_uri``.
_SPOTIFY_URI_RE = re.compile(
    r"spotify(?::|%3[Aa])(track|album|playlist|artist|episode|show)"
    r"(?::|%3[Aa])([A-Za-z0-9]+)"
)

# Spotify's SMAPI service type. Used in the DIDL-Lite ``<desc>`` element
# for container playback. Stable across installs — Sonos uses this to
# look up the linked Spotify account on the target system.
_SPOTIFY_SERVICE_TYPE = 3079

# Map Spotify container kinds to their UPnP class for DIDL metadata. Only
# kinds that Sonos plays as a *container* (multi-track expansion via
# ``AddURIToQueue``) appear here — tracks/episodes/shows stay on the
# direct ``x-sonos-spotify:`` play_uri path.
_SPOTIFY_CONTAINER_CLASS: dict[str, str] = {
    "playlist": "object.container.playlistContainer",
    "album": "object.container.album.musicAlbum",
    "artist": "object.container.person.musicArtist",
}


def _extract_spotify_uri(item_id: str) -> str | None:
    """Recover a clean ``spotify:<kind>:<id>`` URI from a SMAPI item id.

    SoCo's ``MusicService.sonos_uri_from_id`` returns a ``soco://...``
    URI that Sonos' AVTransport refuses with ``UPnP Error 714 Illegal
    MIME-Type`` for Spotify content on modern firmware — the player
    actually needs a real ``x-sonos-spotify:`` URI plus DIDL metadata.
    The good news is SMAPI item ids for the Spotify service embed the
    canonical Spotify URI inside them; we extract it here so the speaker
    backend can rebuild a proper Sonos-Spotify URI via the existing
    ``_to_sonos_spotify_uri`` helper.

    Returns ``None`` if the id doesn't look like it contains a Spotify
    reference — callers should fall back to the SoCo path for non-
    Spotify SMAPI services.
    """
    if not item_id:
        return None
    match = _SPOTIFY_URI_RE.search(item_id)
    if not match:
        return None
    kind, sid = match.group(1), match.group(2)
    return f"spotify:{kind}:{sid}"


def _spotify_kind(item_id: str) -> str | None:
    """Return the Spotify content kind embedded in a SMAPI item id.

    One of ``track|album|playlist|artist|episode|show``, or ``None`` if
    the id doesn't contain a Spotify reference.
    """
    if not item_id:
        return None
    match = _SPOTIFY_URI_RE.search(item_id)
    return match.group(1) if match else None


def _build_spotify_container_playable(item: MusicItem, kind: str) -> Playable:
    """Build a ``Playable`` for a Spotify container (playlist/album/artist).

    Sonos silently no-ops when handed a Spotify container URI via
    ``SetAVTransportURI`` — the AVTransport accepts the call and logs
    success, but nothing plays because a container URI without queue
    context doesn't tell Sonos which track to start. The correct path
    is ``AddURIToQueue`` with an ``x-rincon-cpcontainer:`` URI plus a
    DIDL-Lite envelope carrying the right ``upnp:class`` and Spotify
    service descriptor; Sonos expands the container into individual
    tracks on the queue and then ``play_from_queue`` starts the first.

    The speaker backend sees the ``x-rincon-cpcontainer:`` prefix and
    routes to its queue-playback path (see ``sonos_speaker._play_container``).

    Uses the raw SMAPI item id (with its 8-char flags prefix and
    uppercase ``%3A`` percent-encoding) verbatim as both the URI tail
    and the DIDL ``item id`` — Sonos accepts it exactly as SMAPI
    returned it. Verified experimentally against live hardware:
    ``AddURIToQueue`` returns ``NumTracksAdded=50`` for a typical
    Spotify playlist.
    """
    upnp_class = _SPOTIFY_CONTAINER_CLASS[kind]
    didl = (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
        ' xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"'
        ' xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        f'<item id="{escape(item.id)}" parentID="-1" restricted="true">'
        f"<dc:title>{escape(item.title)}</dc:title>"
        f"<upnp:class>{upnp_class}</upnp:class>"
        '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        f"SA_RINCON{_SPOTIFY_SERVICE_TYPE}_X_#Svc{_SPOTIFY_SERVICE_TYPE}-0-Token"
        "</desc>"
        "</item></DIDL-Lite>"
    )
    return Playable(
        uri=f"x-rincon-cpcontainer:{item.id}",
        didl_meta=didl,
        title=item.title,
    )


def _item_uri(item: Any) -> str:
    resources = getattr(item, "resources", None) or []
    if resources:
        return getattr(resources[0], "uri", "") or ""
    return ""


def _didl_favorite_to_music_item(fav: Any) -> MusicItem:
    """Map a ``DidlFavorite`` to a ``MusicItem``.

    Favorites split into two shapes:

    - Tracks and other playable items: ``reference`` carries a DidlMusicTrack
      (or similar) with a ``resources[0].uri``. We return that URI directly
      and set kind=TRACK/FAVORITE.

    - Containers (radio stations, playlists): ``reference`` is a
      ``DidlContainer`` with no resources. Sonos can still play these via
      ``play_uri`` if given the DIDL metadata envelope from
      ``resource_meta_data``. We return an empty URI and carry the DIDL
      blob so ``resolve_playable`` can pass it through.
    """
    title = getattr(fav, "title", "") or ""
    reference = getattr(fav, "reference", None)
    ref_cls = type(reference).__name__ if reference is not None else ""
    uri = _item_uri(reference) if reference is not None else ""
    didl_meta = getattr(fav, "resource_meta_data", "") or ""

    # Heuristic kind assignment from the reference class name
    if "Track" in ref_cls:
        kind = MusicItemKind.TRACK
    elif "Album" in ref_cls:
        kind = MusicItemKind.ALBUM
    elif "Playlist" in ref_cls:
        kind = MusicItemKind.PLAYLIST
    elif "Station" in ref_cls or "Radio" in ref_cls:
        kind = MusicItemKind.STATION
    else:
        kind = MusicItemKind.FAVORITE

    subtitle = ""
    if reference is not None:
        creator = getattr(reference, "creator", "") or ""
        album = getattr(reference, "album", "") or ""
        subtitle = creator if creator else album

    return MusicItem(
        id=_item_id(fav),
        title=title,
        kind=kind,
        subtitle=subtitle,
        uri=uri,
        didl_meta=didl_meta,
        service="Sonos Favorites",
    )


def _didl_playlist_to_music_item(pl: Any) -> MusicItem:
    """Map a ``DidlPlaylistContainer`` (a saved Sonos playlist) to a ``MusicItem``."""
    return MusicItem(
        id=_item_id(pl),
        title=getattr(pl, "title", "") or "",
        kind=MusicItemKind.PLAYLIST,
        subtitle=getattr(pl, "creator", "") or "",
        uri=_item_uri(pl),
        didl_meta=getattr(pl, "resource_meta_data", "") or "",
        service="Sonos Playlists",
    )


def _smapi_metadata(item: Any) -> dict[str, Any]:
    """Return the ``.metadata`` dict from a SMAPI result, or ``{}``.

    SoCo SMAPI result objects carry all their fields (id, title, artist,
    album_art_uri, ...) on ``item.metadata`` — the instance itself
    doesn't expose them via attribute access, so accidental
    ``getattr(item, "artist", None)`` comes back empty. Use this helper
    to walk the nested dict instead.
    """
    md = getattr(item, "metadata", None)
    return md if isinstance(md, dict) else {}


def _smapi_track_metadata(md: dict[str, Any]) -> dict[str, Any]:
    """Return the nested ``track_metadata`` dict for a SMAPI track result.

    Tracks wrap their album/artist/artwork inside a ``TrackMetadata``
    object whose ``.metadata`` attribute is the actual dict — playlists
    and albums inline those fields at the top of ``item.metadata``, so
    the per-kind paths differ. Returns ``{}`` when absent or malformed.
    """
    tm = md.get("track_metadata")
    if tm is None:
        return {}
    inner = getattr(tm, "metadata", None)
    return inner if isinstance(inner, dict) else {}


def _smapi_item_art(item: Any) -> str:
    """Best-effort album art URL extraction from a SMAPI search result.

    Playlists and albums expose ``album_art_uri`` at the top of
    ``item.metadata``. Tracks nest it inside ``track_metadata.metadata``.
    Returns an empty string when no artwork is available.
    """
    md = _smapi_metadata(item)
    art = md.get("album_art_uri") or _smapi_track_metadata(md).get("album_art_uri")
    return str(art) if art else ""


def _smapi_item_subtitle(item: Any) -> str:
    """Best-effort artist/creator extraction for a SMAPI search result.

    Falls back through several fields because SMAPI uses different shapes
    per kind: playlists have ``artist`` (the curator, e.g. "Spotify"),
    albums have ``artist``, tracks have it inside ``track_metadata``.
    Returns an empty string when nothing useful is available.
    """
    md = _smapi_metadata(item)
    val = md.get("artist") or md.get("creator")
    if not val:
        val = _smapi_track_metadata(md).get("artist")
    return str(val) if val else ""


def _smapi_item_duration(item: Any) -> float:
    """Duration in seconds for a SMAPI track result, or 0.0.

    Only tracks carry duration — playlists/albums are containers.
    """
    tm = _smapi_track_metadata(_smapi_metadata(item))
    dur = tm.get("duration")
    if isinstance(dur, int | float):
        return float(dur)
    return 0.0


def _smapi_result_to_music_item(
    item: Any,
    kind: MusicItemKind,
    service_name: str,
) -> MusicItem:
    """Map a SMAPI search result row to a ``MusicItem``.

    SMAPI results carry an opaque ``item_id`` that has to be resolved to
    a playable URI via ``sonos_uri_from_id`` later — ``uri`` is left empty
    here on purpose. Artwork, artist, and duration are pulled from the
    result's ``metadata`` dict (shape varies by kind; see helpers).
    """
    md = _smapi_metadata(item)
    title = getattr(item, "title", "") or md.get("title") or "(unknown)"
    return MusicItem(
        id=_item_id(item),
        title=str(title),
        kind=kind,
        subtitle=_smapi_item_subtitle(item),
        uri="",
        album_art_url=_smapi_item_art(item),
        duration_seconds=_smapi_item_duration(item),
        service=service_name,
    )


class SonosMusic(MusicBackend):
    """Music backend backed entirely by the Sonos system.

    Favorites and playlists work out of the box. Search requires a
    one-time SMAPI auth flow (see ``backend_actions``).
    """

    backend_name = "sonos"

    @classmethod
    def backend_config_params(cls) -> list[Any]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="preferred_service",
                type=ToolParameterType.STRING,
                description=(
                    "Which linked music service to search against "
                    "(e.g. Spotify, Apple Music). Dropdown is populated "
                    "with services currently linked on your Sonos system."
                ),
                default="Spotify",
                choices_from="music_services",
            ),
            ConfigParam(
                key="auth_token",
                type=ToolParameterType.STRING,
                description=(
                    "SMAPI search token. Obtained via the 'Link Spotify "
                    "for search' action — do not edit by hand."
                ),
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="auth_key",
                type=ToolParameterType.STRING,
                description=(
                    "SMAPI search token key, paired with auth_token. "
                    "Obtained via the 'Link Spotify for search' action."
                ),
                default="",
                sensitive=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="link_spotify",
                label="Link music service for search",
                description=(
                    "Authorize Gilbert to search your preferred music "
                    "service via your Sonos system. One-time setup, "
                    "required before /music search and the radio DJ can "
                    "discover music."
                ),
            ),
            # Hidden second phase of the link flow. The UI never renders
            # a button for it — the first-phase action returns a
            # ``followup_action`` pointing at this key, and the UI
            # relabels the existing button to "Continue". This entry
            # exists so the RPC's action lookup (by key, for RBAC) finds
            # it instead of 404'ing.
            ConfigAction(
                key="link_spotify_complete",
                label="Complete link flow",
                description="Finish the SMAPI link flow started by link_spotify.",
                hidden=True,
            ),
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Verify that Sonos speakers are reachable and (if "
                    "configured) the search auth token still works."
                ),
            ),
        ]

    def __init__(self) -> None:
        self._preferred_service: str = "Spotify"
        self._token_store = _InMemoryTokenStore()
        self._devices: list[SoCo] = []
        self._smapi: SonosSMAPI | None = None
        self._pending_link: dict[str, Any] | None = None

    # --- Lifecycle ---

    async def initialize(self, config: dict[str, object]) -> None:
        self._preferred_service = str(
            config.get("preferred_service") or "Spotify",
        )
        token = str(config.get("auth_token") or "")
        key = str(config.get("auth_key") or "")
        self._token_store = _InMemoryTokenStore(token=token, key=key)
        self._smapi = None  # Force re-creation with the new token store

        self._devices = await asyncio.to_thread(self._discover_sync)
        logger.info(
            "Sonos music backend initialized (service=%s, devices=%d, authed=%s)",
            self._preferred_service,
            len(self._devices),
            bool(token),
        )

    async def close(self) -> None:
        self._devices = []
        self._smapi = None

    def _discover_sync(self) -> list[SoCo]:
        found = soco.discover()
        return list(found) if found else []

    def _require_device(self) -> SoCo:
        if not self._devices:
            self._devices = self._discover_sync()
        if not self._devices:
            raise RuntimeError("No Sonos speakers found on the network")
        return self._devices[0]

    # --- Service discovery ---

    def list_linked_services(self) -> list[str]:
        """Return names of music services currently linked on the Sonos system.

        Tries three sources in order and unions them, mapping the
        Sonos-internal numeric ``ServiceType`` back to human names:

        1. ``Account.get_accounts()`` — the documented API, but not
           consistently populated on every household (tested empty on
           production systems where the accounts panel clearly shows
           linked services).
        2. DIDL meta on every favorite — matches ``SA_RINCON<type>_``
           in the ``resource_meta_data`` blob. This catches anything
           the user has actually used.
        3. Resource URIs on every favorite — ``sid=`` query param as a
           last-ditch signal for service ids.

        Falls back to an empty list if discovery fails; the caller
        should degrade to letting the user type a free-form name.
        """
        try:
            device = self._require_device()
        except RuntimeError:
            return []

        service_types: set[int] = set()

        # Source 1: accounts API
        try:
            from soco.music_services.accounts import Account

            for acct in Account.get_accounts().values():
                try:
                    st = int(acct.service_type)
                except (TypeError, ValueError):
                    continue
                if st:
                    service_types.add(st)
        except Exception:
            logger.debug("Account.get_accounts() failed", exc_info=True)

        # Source 2: favorites DIDL metadata
        try:
            favs = device.music_library.get_sonos_favorites()
            for fav in favs:
                meta = getattr(fav, "resource_meta_data", "") or ""
                for m in re.finditer(r"SA_RINCON(\d+)_", meta):
                    try:
                        service_types.add(int(m.group(1)))
                    except ValueError:
                        continue
        except Exception:
            logger.debug("Favorites-based service discovery failed", exc_info=True)

        if not service_types:
            return []

        # Map numeric ServiceType → name via SoCo's service catalog.
        # Iterating all ~100 names once at startup is cheap.
        try:
            all_names = SonosSMAPI.get_all_music_services_names()
        except Exception:
            logger.debug("get_all_music_services_names() failed", exc_info=True)
            return []

        linked: list[str] = []
        for name in all_names:
            try:
                data = SonosSMAPI.get_data_for_name(name)
            except Exception:
                continue
            try:
                st = int(data.get("ServiceType", 0))
            except (TypeError, ValueError):
                continue
            if st in service_types and name not in linked:
                linked.append(name)

        return sorted(linked)

    def _get_smapi(self) -> SonosSMAPI:
        """Lazily build the SoCo ``MusicService`` handle for the preferred service."""
        if self._smapi is not None:
            return self._smapi
        device = self._require_device()
        self._smapi = SonosSMAPI(
            self._preferred_service,
            token_store=self._token_store,
            device=device,
        )
        return self._smapi

    # --- Browse ---

    async def list_favorites(self) -> list[MusicItem]:
        def _fetch() -> list[MusicItem]:
            device = self._require_device()
            results = device.music_library.get_sonos_favorites()
            return [_didl_favorite_to_music_item(f) for f in results]

        return await asyncio.to_thread(_fetch)

    async def list_playlists(self) -> list[MusicItem]:
        def _fetch() -> list[MusicItem]:
            device = self._require_device()
            results = device.get_sonos_playlists()
            return [_didl_playlist_to_music_item(p) for p in results]

        return await asyncio.to_thread(_fetch)

    # --- Search ---

    async def search(
        self,
        query: str,
        *,
        kind: MusicItemKind = MusicItemKind.TRACK,
        limit: int = 10,
    ) -> list[MusicItem]:
        smapi_kind = _KIND_TO_SMAPI.get(kind)
        if smapi_kind is None:
            raise ValueError(f"Unsupported search kind: {kind}")

        def _search() -> list[MusicItem]:
            svc = self._get_smapi()
            results = svc.search(smapi_kind, query, count=limit)
            return [_smapi_result_to_music_item(r, kind, self._preferred_service) for r in results]

        try:
            return await asyncio.to_thread(_search)
        except Exception as exc:
            # Distinguish auth errors from transient ones
            from soco.exceptions import MusicServiceAuthException

            if isinstance(exc, MusicServiceAuthException):
                raise MusicSearchUnavailableError(
                    f"{self._preferred_service} search isn't authenticated yet. "
                    "An admin needs to run the 'Link music service for search' "
                    "action in Settings → Media → Music."
                ) from exc
            logger.warning("Sonos music search failed: %s", exc, exc_info=True)
            raise

    # --- Playback resolution ---

    async def resolve_playable(self, item: MusicItem) -> Playable:
        # Favorites / playlists with a direct URI: pass through, carrying
        # DIDL meta along if present (needed by some container-style
        # favorites so Sonos can render the metadata envelope).
        if item.uri:
            return Playable(
                uri=item.uri,
                didl_meta=item.didl_meta,
                title=item.title,
            )
        # Container-only favorites (no URI, DIDL blob only): pass the meta
        # through. The speaker backend must be capable of playing from
        # DIDL alone for these to actually start.
        if item.didl_meta:
            return Playable(
                uri="",
                didl_meta=item.didl_meta,
                title=item.title,
            )
        # Search results: resolve the opaque id via SMAPI
        if not item.id:
            raise ValueError(f"MusicItem has no uri and no id: {item.title}")

        # Fast path for Spotify containers (playlists/albums/artists):
        # ``SetAVTransportURI`` with a container URI silently no-ops —
        # the transport accepts the call but no audio plays. Route
        # containers through an ``x-rincon-cpcontainer:`` URI plus a
        # DIDL envelope that the speaker backend will enqueue via
        # ``AddURIToQueue`` and then ``play_from_queue``.
        kind = _spotify_kind(item.id)
        if kind is not None and kind in _SPOTIFY_CONTAINER_CLASS:
            return _build_spotify_container_playable(item, kind)

        # Fast path for Spotify single items (tracks/episodes/shows):
        # the SMAPI item id embeds the canonical ``spotify:<kind>:<id>``
        # URI. Handing that straight through to the speaker backend
        # lets it build a real ``x-sonos-spotify:`` URI, which is the
        # shape Sonos' AVTransport actually accepts for single items.
        # ``sonos_uri_from_id`` returns ``soco://...``, which modern
        # firmware rejects as "Illegal MIME-Type" (UPnP 714).
        spotify_uri = _extract_spotify_uri(item.id)
        if spotify_uri is not None:
            return Playable(uri=spotify_uri, didl_meta="", title=item.title)

        # Fallback for non-Spotify SMAPI services. ``sonos_uri_from_id``
        # is documented to work for them, though it has only been
        # verified in practice for a handful of services; if your
        # service returns 714 here the fix is to teach this method how
        # to extract its canonical URI in the same way we do for Spotify.
        def _resolve() -> str:
            svc = self._get_smapi()
            return str(svc.sonos_uri_from_id(item.id))

        uri = await asyncio.to_thread(_resolve)
        return Playable(uri=uri, didl_meta="", title=item.title)

    # --- Actions ---

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "link_spotify":
            return await self._action_link_start()
        if key == "link_spotify_complete":
            return await self._action_link_complete()
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_link_start(self) -> ConfigActionResult:
        def _begin() -> tuple[str, Any, Any]:
            svc = self._get_smapi()
            url = svc.begin_authentication()
            return url, svc.link_code, svc.link_device_id

        try:
            url, link_code, link_device_id = await asyncio.to_thread(_begin)
        except Exception as exc:
            logger.exception("begin_authentication failed")
            return ConfigActionResult(
                status="error",
                message=f"Couldn't start link flow: {exc}",
            )

        # Hold the link_code/link_device_id in memory until the follow-up
        # click arrives. The SMAPI object does this too, but a fresh
        # instance on the next call wouldn't inherit it, so we cache
        # explicitly.
        self._pending_link = {
            "link_code": link_code,
            "link_device_id": link_device_id,
        }

        return ConfigActionResult(
            status="pending",
            message=(
                "1) Open the link below and approve access. "
                "2) Return here and click Continue to finish linking."
            ),
            open_url=url,
            followup_action="link_spotify_complete",
        )

    async def _action_link_complete(self) -> ConfigActionResult:
        if self._pending_link is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "No link flow in progress. Click 'Link music service for search' to start over."
                ),
            )
        link_code = self._pending_link["link_code"]
        link_device_id = self._pending_link["link_device_id"]

        def _complete() -> tuple[str, str]:
            svc = self._get_smapi()
            svc.link_code = link_code
            svc.link_device_id = link_device_id
            svc.complete_authentication()
            return self._token_store.token, self._token_store.key

        try:
            token, key = await asyncio.to_thread(_complete)
        except Exception as exc:
            logger.exception("complete_authentication failed")
            self._pending_link = None
            return ConfigActionResult(
                status="error",
                message=(f"Couldn't complete link flow (did you approve in the browser?): {exc}"),
            )

        self._pending_link = None
        if not token:
            return ConfigActionResult(
                status="error",
                message=("Link flow completed but no token was issued. Try starting over."),
            )

        # Push the new token/key into the settings form as unsaved
        # changes via the ``persist`` side-channel. The UI receives them
        # in ``result.data["persist"]`` and drops them into its local
        # form state so the Auth Token / Auth Key fields fill in and
        # Save activates — the user then clicks Save to actually store
        # them. (Auto-saving from here would bypass the user's expected
        # save flow and leave Save disabled, which is confusing.)
        return ConfigActionResult(
            status="ok",
            message=(
                f"{self._preferred_service} linked for search. Click Save to store the auth token."
            ),
            data={
                "persist": {
                    "settings.auth_token": token,
                    "settings.auth_key": key,
                },
            },
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        try:
            devices = await asyncio.to_thread(self._discover_sync)
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Sonos discovery failed: {exc}",
            )
        if not devices:
            return ConfigActionResult(
                status="error",
                message="No Sonos speakers found on the network.",
            )
        self._devices = devices

        # If an auth token is present, try a tiny search to verify it
        if self._token_store.has_token(None, None):
            try:
                await self.search("test", kind=MusicItemKind.TRACK, limit=1)
                return ConfigActionResult(
                    status="ok",
                    message=(
                        f"Found {len(devices)} speakers. "
                        f"{self._preferred_service} search is working."
                    ),
                )
            except MusicSearchUnavailableError as exc:
                return ConfigActionResult(
                    status="error",
                    message=str(exc),
                )
            except Exception as exc:
                return ConfigActionResult(
                    status="error",
                    message=(f"Found {len(devices)} speakers, but search failed: {exc}"),
                )

        return ConfigActionResult(
            status="ok",
            message=(
                f"Found {len(devices)} speakers. Search is not linked yet "
                "— click 'Link music service for search' to enable it."
            ),
        )
