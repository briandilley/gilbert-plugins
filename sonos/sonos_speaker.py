"""Sonos speaker backend — S2 local WebSocket API via aiosonos.

Replaces the previous SoCo implementation. aiosonos speaks Sonos's
S2-only local WebSocket API on port 1443 — event-driven, declarative
grouping, native short-clip announcements (no snapshot/restore dance,
the ``audio_clip`` API auto-ducks + auto-restores). S1 speakers are
NOT supported; run ``scripts/check_sonos_s2.py`` before relying on
this plugin.

Discovery is handled by zeroconf (Sonos advertises on
``_sonos._tcp.local.``). Each discovered speaker gets a dedicated
``SonosLocalApiClient`` connection — that's how aiosonos is designed
in Music Assistant (its parent project): one client per player.
Player-level operations go through that client's ``player`` object;
group-level operations can be invoked through any client in the
same household.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from dataclasses import dataclass
from typing import Any

import aiohttp
import httpx
from aiosonos import SonosLocalApiClient
from aiosonos.api.models import (
    LoadContentRequest,
    MetadataId,
    MusicService as AioMusicService,
    PlayBackState,
)
from aiosonos.exceptions import (
    CannotConnect,
    ConnectionClosed,
    ConnectionFailed,
    FailedCommand,
    SonosException,
)
from aiosonos.utils import get_discovery_info
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from gilbert.interfaces.configuration import ConfigAction, ConfigActionResult
from gilbert.interfaces.speaker import (
    NowPlaying,
    PlaybackState,
    PlayRequest,
    SpeakerBackend,
    SpeakerGroup,
    SpeakerInfo,
)

logger = logging.getLogger(__name__)

# Sonos speakers advertise over mDNS as ``_sonos._tcp.local.``. Zeroconf
# fires a ServiceStateChange event for each discovered instance — we
# don't need to broadcast SSDP ourselves.
_SONOS_SERVICE_TYPE = "_sonos._tcp.local."
_DISCOVERY_SETTLE_SECONDS = 3.0
_CONNECT_TIMEOUT = 10.0
_INFO_PROBE_TIMEOUT = 5.0

# Spotify URIs as they appear in MusicItem.uri: ``spotify:track:abc123``
# etc. We route these to ``playback.load_content`` with a Spotify
# ``MetadataId`` rather than ``load_stream_url``, because Sonos plays
# Spotify content through the speaker's linked account rather than as
# a plain HTTP stream.
_SPOTIFY_URI_RE = re.compile(
    r"^spotify:(track|album|playlist|artist|episode|show):([A-Za-z0-9]+)$"
)
_SPOTIFY_OPEN_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)"
)

# Sonos publishes this local-API token in every S2 speaker's firmware.
# Not a secret — aiosonos itself uses it. Gates the info endpoint
# against casual abuse, nothing more.
_LOCAL_API_KEY = "123e4567-e89b-12d3-a456-426655440000"
_LOCAL_INFO_URL = "https://{ip}:1443/api/v1/players/local/info"

# Map aiosonos PlayBackState values to our PlaybackState enum.
_PLAYBACK_STATE_MAP: dict[str, PlaybackState] = {
    PlayBackState.PLAYBACK_STATE_PLAYING.value: PlaybackState.PLAYING,
    PlayBackState.PLAYBACK_STATE_PAUSED.value: PlaybackState.PAUSED,
    PlayBackState.PLAYBACK_STATE_IDLE.value: PlaybackState.STOPPED,
    PlayBackState.PLAYBACK_STATE_BUFFERING.value: PlaybackState.TRANSITIONING,
}

# Audio-clip max length per Sonos's own API documentation. Anything
# longer gets truncated. We don't enforce it on the Gilbert side —
# announcements that fit comfortably don't need the ceiling, and
# callers sending longer URLs get a clean Sonos-side error if it
# exceeds.
_AUDIO_CLIP_MAX_SECONDS = 60


@dataclass
class _PlayerMetadata:
    """Per-player info cached from zeroconf + the info endpoint.

    aiosonos's ``SonosPlayer`` is tied to an open WebSocket connection;
    we keep the static identity fields here so the plugin can list /
    look up speakers without touching the live client.
    """

    player_id: str
    household_id: str
    name: str
    ip_address: str
    model: str


class SonosSpeaker(SpeakerBackend):
    """Sonos speaker backend driven by aiosonos (S2 local WebSocket)."""

    backend_name = "sonos"

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Run Sonos zeroconf discovery and report how many "
                    "S2 speakers responded with a valid local-API info "
                    "endpoint."
                ),
            ),
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict,
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        count = len(self._player_metadata)
        households = {pm.household_id for pm in self._player_metadata.values()}
        connected = sum(1 for c in self._clients.values() if c is not None)
        if count == 0:
            return ConfigActionResult(
                status="error",
                message=(
                    "No Sonos speakers discovered yet. Zeroconf discovery "
                    "runs in the background on backend start; try again "
                    "in a few seconds, or check that multicast isn't "
                    "blocked on your LAN."
                ),
            )
        return ConfigActionResult(
            status="ok",
            message=(
                f"{count} Sonos speaker(s) discovered across "
                f"{len(households)} household(s); {connected} WebSocket "
                f"connection(s) live."
            ),
        )

    def __init__(self) -> None:
        # One aiosonos client per discovered player. The client owns a
        # persistent WebSocket connection to *its* speaker and dispatches
        # commands through it; group-level commands target the group by
        # id rather than the coordinator player.
        self._clients: dict[str, SonosLocalApiClient] = {}
        self._player_metadata: dict[str, _PlayerMetadata] = {}
        self._zeroconf: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        # Background tasks — ``start_listening`` is long-running per
        # client and needs to be cancelled on shutdown.
        self._listen_tasks: dict[str, asyncio.Task[Any]] = {}
        # aiohttp session reused for both zeroconf probes and aiosonos
        # client construction — avoids one-off session churn and
        # (important) lets us pre-install the Sonos self-signed cert
        # bypass once instead of per-probe.
        self._http_session: aiohttp.ClientSession | None = None
        # Lock so zeroconf callbacks don't race against each other.
        self._discovery_lock = asyncio.Lock()
        # IPs we've already brought up (or decided to skip) — zeroconf
        # fires Added/Updated repeatedly as records refresh, and we
        # don't want to re-probe the info endpoint + reconnect every
        # time. The set survives for the lifetime of the backend since
        # a Sonos speaker's IP+identity binding is stable across
        # mDNS refreshes.
        self._known_ips: set[str] = set()

    # ── Lifecycle ────────────────────────────────────────────────────

    async def initialize(self, config: dict[str, object]) -> None:
        """Start zeroconf discovery and kick off an initial settle wait.

        aiosonos has no LAN-scan helper — we depend on Sonos's own mDNS
        advertisements. Zeroconf fires service-add events as speakers
        respond, and ``_on_service_state_change`` resolves each one and
        creates a client connection. The ``settle`` wait gives the
        initial batch of speakers time to advertise before the caller
        starts making requests; it's not load-bearing (subsequent
        speakers are still picked up asynchronously).
        """
        # Self-signed cert context used for both HTTPS probes and the
        # aiosonos WebSocket. Sonos speakers ship with untrusted certs;
        # verifying them doesn't add security on a LAN-only control
        # plane and would just break every connection. aiosonos itself
        # passes ``ssl=False`` internally, but we use a context for our
        # own httpx probes.
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

        self._http_session = aiohttp.ClientSession()

        self._zeroconf = AsyncZeroconf()
        self._browser = AsyncServiceBrowser(
            self._zeroconf.zeroconf,
            _SONOS_SERVICE_TYPE,
            handlers=[self._on_service_state_change],
        )

        # Wait for the initial wave of advertisements so the first
        # ``list_speakers`` call isn't empty. Callers that need to
        # ensure discovery is complete can poll or await a longer
        # timeout — this is just a best-effort settle.
        await asyncio.sleep(_DISCOVERY_SETTLE_SECONDS)

        logger.info(
            "Sonos backend initialized — %d speaker(s) discovered in %.1fs",
            len(self._player_metadata),
            _DISCOVERY_SETTLE_SECONDS,
        )

    async def close(self) -> None:
        """Tear down all connections + discovery."""
        if self._browser is not None:
            await self._browser.async_cancel()
            self._browser = None

        # Cancel long-running listener tasks before disconnecting —
        # otherwise disconnect races the listener and we log spurious
        # ConnectionClosed errors.
        for task in self._listen_tasks.values():
            task.cancel()
        if self._listen_tasks:
            await asyncio.gather(
                *self._listen_tasks.values(), return_exceptions=True
            )
        self._listen_tasks.clear()

        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("Error disconnecting Sonos client", exc_info=True)
        self._clients.clear()

        if self._zeroconf is not None:
            await self._zeroconf.async_close()
            self._zeroconf = None

        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

        self._player_metadata.clear()

    # ── Discovery ────────────────────────────────────────────────────

    def _on_service_state_change(
        self,
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Zeroconf callback — schedule resolution of the changed service.

        Zeroconf delivers events synchronously from its own thread, so
        we schedule an async handler on the main loop rather than doing
        I/O here. Additions and updates both probe the speaker; removals
        drop the cached metadata.
        """
        if state_change == ServiceStateChange.Removed:
            asyncio.create_task(self._handle_service_removed(name))
            return
        if state_change in (
            ServiceStateChange.Added,
            ServiceStateChange.Updated,
        ):
            asyncio.create_task(
                self._handle_service_added(zeroconf, service_type, name)
            )

    async def _handle_service_added(
        self,
        zeroconf: Any,
        service_type: str,
        service_name: str,
    ) -> None:
        """Resolve an mDNS record and bring up a client for the speaker."""
        async with self._discovery_lock:
            info = AsyncServiceInfo(service_type, service_name)
            try:
                resolved = await info.async_request(zeroconf, 3000)
            except Exception:  # noqa: BLE001 - log and drop
                logger.debug(
                    "Zeroconf resolve failed for %s", service_name, exc_info=True
                )
                return
            if not resolved or not info.addresses:
                return

            # Zeroconf returns IPv4 addresses as packed 4-byte strings —
            # convert to dotted-quad strings for the info endpoint + WS.
            ip = ".".join(str(b) for b in info.addresses[0])
            await self._bring_up_speaker(ip)

    async def _handle_service_removed(self, service_name: str) -> None:
        """Clean up state when zeroconf reports a speaker has gone.

        Removal is best-effort — Sonos speakers often advertise
        ephemerally and come back under the same name. We don't tear
        down the client eagerly; the listener task will notice the
        WebSocket closing and we'll reconnect on the next Add event.
        """
        logger.debug("Zeroconf reported service removal: %s", service_name)

    async def _bring_up_speaker(self, ip: str) -> None:
        """Probe the S2 info endpoint, then open an aiosonos client.

        Idempotent by IP: zeroconf re-fires Added/Updated for the same
        speaker as its mDNS records refresh, and we don't want to
        re-probe + reconnect on every firing.
        """
        if ip in self._known_ips:
            return
        self._known_ips.add(ip)
        # Probe /api/v1/players/local/info — this gives us the stable
        # playerId + householdId identifiers that aiosonos expects,
        # plus model/name for UI listings.
        metadata = await self._probe_player(ip)
        if metadata is None:
            return
        if metadata.player_id in self._player_metadata:
            # Already known — idempotent on repeated mDNS events.
            return

        self._player_metadata[metadata.player_id] = metadata

        client = SonosLocalApiClient(ip, self._http_session)
        try:
            await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        except (asyncio.TimeoutError, CannotConnect, ConnectionFailed) as exc:
            logger.warning(
                "Failed to connect to Sonos speaker %s (%s): %s",
                metadata.name,
                ip,
                exc,
            )
            self._player_metadata.pop(metadata.player_id, None)
            return

        self._clients[metadata.player_id] = client

        # aiosonos's ``start_listening`` is typed as accepting an
        # optional ``init_ready: asyncio.Event | None = None`` but then
        # unconditionally calls ``init_ready.set()`` at the end of
        # initial setup — so we MUST pass an Event or it raises
        # ``AttributeError: 'NoneType' object has no attribute 'set'``
        # and the listener task dies before dispatching any events.
        # The Event is useful beyond the bug-workaround anyway: it
        # signals "initial household state is loaded" so a request
        # arriving right after ``_bring_up_speaker`` returns doesn't
        # race against an empty ``client.groups``.
        init_ready = asyncio.Event()

        async def _listen() -> None:
            # ``start_listening`` fetches initial state + keeps the
            # connection alive, dispatching push events to subscribers.
            # Runs until the WebSocket closes or the task is cancelled.
            try:
                await client.start_listening(init_ready)
            except (ConnectionClosed, SonosException):
                logger.debug(
                    "Sonos listener for %s closed", metadata.name, exc_info=True
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - report once and exit
                logger.exception(
                    "Unexpected error in Sonos listener for %s", metadata.name
                )

        task = asyncio.create_task(
            _listen(), name=f"sonos-listen-{metadata.player_id}"
        )
        self._listen_tasks[metadata.player_id] = task

        # Wait (bounded) for initial setup so callers see populated
        # groups/player state when they start querying. If the handshake
        # stalls we still let discovery continue — the speaker just
        # won't be usable until it catches up.
        try:
            await asyncio.wait_for(init_ready.wait(), timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "Sonos speaker '%s' (%s) didn't complete initial setup in "
                "%.1fs — marking as degraded",
                metadata.name,
                ip,
                _CONNECT_TIMEOUT,
            )

        logger.info(
            "Connected to Sonos speaker '%s' (%s, %s)",
            metadata.name,
            metadata.model,
            ip,
        )

    async def _probe_player(self, ip: str) -> _PlayerMetadata | None:
        """Hit the S2 info endpoint to extract identity fields."""
        url = _LOCAL_INFO_URL.format(ip=ip)
        headers = {"X-Sonos-Api-Key": _LOCAL_API_KEY}
        try:
            async with httpx.AsyncClient(
                verify=self._ssl_ctx, timeout=_INFO_PROBE_TIMEOUT
            ) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError:
            logger.debug("S2 info probe failed for %s", ip, exc_info=True)
            return None

        if resp.status_code != 200:
            logger.debug(
                "S2 info probe %s returned HTTP %d", ip, resp.status_code
            )
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        player_id = str(data.get("playerId") or "")
        household_id = str(data.get("householdId") or "")
        if not player_id or not household_id:
            # Some S2 firmwares omit playerId on the info endpoint and
            # require the caller to discover it via the WebSocket
            # handshake. Fall back to the aiosonos helper which does
            # exactly that.
            try:
                discovery = await get_discovery_info(
                    self._require_http_session(), ip
                )
            except Exception:
                logger.debug(
                    "get_discovery_info fallback failed for %s", ip, exc_info=True
                )
                return None
            player_id = str(discovery.get("playerId", player_id) or "")
            household_id = str(
                discovery.get("householdId", household_id) or ""
            )
            if not player_id or not household_id:
                return None

        return _PlayerMetadata(
            player_id=player_id,
            household_id=household_id,
            name=str(data.get("device", {}).get("name", "") or "Unknown"),
            ip_address=ip,
            model=str(data.get("device", {}).get("model", "") or ""),
        )

    def _require_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None:
            raise RuntimeError("Sonos backend not initialized")
        return self._http_session

    # ── Discovery API ────────────────────────────────────────────────

    async def list_speakers(self) -> list[SpeakerInfo]:
        """Materialize SpeakerInfo for every known speaker.

        Pulls live volume + group membership from the client for each
        player, so results reflect the current state of the system
        (not a stale snapshot taken at discovery time).
        """
        infos: list[SpeakerInfo] = []
        for player_id, meta in self._player_metadata.items():
            client = self._clients.get(player_id)
            volume = 0
            group_id = ""
            group_name = ""
            is_coord = False
            state = PlaybackState.STOPPED
            if client is not None:
                player = client.player
                volume = int(player.volume_level or 0)
                group = player.group
                if group is not None:
                    group_id = group.id
                    group_name = group.name or ""
                    is_coord = player.is_coordinator
                    state = _PLAYBACK_STATE_MAP.get(
                        str(group.playback_state or ""),
                        PlaybackState.STOPPED,
                    )
            infos.append(
                SpeakerInfo(
                    speaker_id=player_id,
                    name=meta.name,
                    ip_address=meta.ip_address,
                    model=meta.model,
                    group_id=group_id,
                    group_name=group_name,
                    is_group_coordinator=is_coord,
                    volume=volume,
                    state=state,
                )
            )
        infos.sort(key=lambda s: s.name.lower())
        return infos

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        if speaker_id not in self._player_metadata:
            return None
        # Reuse list_speakers's per-speaker materialization — small
        # enough to be fine, and keeps state-derivation in one place.
        infos = await self.list_speakers()
        return next((i for i in infos if i.speaker_id == speaker_id), None)

    # ── Playback ─────────────────────────────────────────────────────

    async def play_uri(self, request: PlayRequest) -> None:
        """Play an audio URI on the requested speakers.

        Dispatch table:

        - ``request.announce=True`` → ``player.play_audio_clip``. Native
          duck-and-restore on the speaker; ideal for TTS.
        - HTTP(S) URL → ``group.play_stream_url``. Sonos probes the
          URL's Content-Type and picks the right decoder.
        - ``spotify:…`` URI or ``open.spotify.com`` link → raises
          ``NotImplementedError``. Spotify playback via the local API
          requires a ``MetadataId`` with an ``accountId`` discovered
          from the speaker's linked-services list — that lookup lives
          in the music backend (Phase 2 of the aiosonos migration), not
          here. Callers should route Spotify content through
          ``MusicService.play`` / ``MusicBackend.resolve_playable``
          rather than ``SpeakerService.play_on_speakers``.
        """
        target_ids = request.speaker_ids or list(self._player_metadata.keys())
        if not target_ids:
            raise RuntimeError("No speakers available")

        # Verify each target is actually connected before attempting
        # to play — otherwise we get misleading KeyErrors. Dedupe while
        # preserving order: callers sometimes pass the same player_id
        # twice (e.g. a speaker resolved via both its real name and an
        # alias), and Sonos rejects ``set_group_members`` with
        # "Effective set of new group members has repeated player id"
        # if duplicates survive that far.
        seen: set[str] = set()
        live: list[str] = []
        for tid in target_ids:
            if tid in seen or tid not in self._clients:
                continue
            seen.add(tid)
            live.append(tid)
        if not live:
            raise RuntimeError(
                f"None of the requested speakers ({target_ids}) are connected"
            )

        if request.announce:
            await self._play_audio_clip(live, request)
            return

        # Form a group if needed. aiosonos's declarative model: just
        # ask for exactly these players in a group. No join/unjoin
        # rodeo, no UPnP 800 retry logic.
        coord_player_id = await self._ensure_group(live)
        coord_client = self._clients[coord_player_id]
        group = coord_client.player.group
        if group is None:
            raise RuntimeError(
                f"Coordinator speaker {coord_player_id} has no active group"
            )

        # Set volume across the group before loading content so the
        # playback opens at the intended level (Sonos will apply the
        # volume to the next buffer, which is usually fast enough that
        # nobody hears the wrong level).
        if request.volume is not None:
            await self._set_group_volume(live, request.volume)

        spotify = _extract_spotify_ref(request.uri)
        if spotify is not None:
            await self._load_spotify_content(
                coord_client, group.id, spotify, request.title
            )
            return

        # Generic HTTP(S) stream — let Sonos probe the URL and pick.
        # ``group.play_stream_url`` requires a Container metadata object
        # (it calls ``load_stream_url(station_metadata=...)`` under the
        # hood with ``play_on_completion=True``). Build a minimal
        # station-type Container; Sonos uses ``name`` for its "now
        # playing" string and derives everything else from the HTTP
        # response headers.
        metadata: dict[str, Any] = {
            "_objectType": "container",
            "name": request.title or "Gilbert audio",
            "type": "station",
        }
        await group.play_stream_url(request.uri, metadata)

    async def _load_spotify_content(
        self,
        client: SonosLocalApiClient,
        group_id: str,
        spotify: _SpotifyRef,
        title: str,
    ) -> None:
        """Load Spotify content via the speaker's linked account.

        The speaker's own Spotify binding (configured through the
        Sonos mobile app, not Gilbert) handles the actual streaming —
        Gilbert just tells it *which* Spotify URI to play using the
        household's SPOTIFY service entry. ``accountId`` is omitted;
        Sonos resolves the default linked Spotify account on the
        household, which is correct for the typical single-account
        household we target.
        """
        content_type = _SPOTIFY_KIND_TO_LOAD_TYPE.get(spotify.kind)
        if content_type is None:
            raise ValueError(
                f"Unsupported Spotify content kind for playback: {spotify.kind}"
            )
        metadata_id: MetadataId = {
            "serviceId": str(AioMusicService.SPOTIFY.value),
            "objectId": spotify.uri,
        }
        request: LoadContentRequest = {
            "type": content_type,
            "id": metadata_id,
            "playbackAction": "PLAY",
        }
        await client.api.playback.load_content(group_id, request)

    async def _play_audio_clip(
        self,
        speaker_ids: list[str],
        request: PlayRequest,
    ) -> None:
        """Fire a short overlay clip on each target speaker.

        Sonos's ``audio_clip`` API is single-speaker — the speaker itself
        handles the duck + restore. For multi-speaker announcements we
        just fire the clip on every target in parallel; the sync is
        good enough that listeners don't hear drift on short clips.
        """
        volume = request.volume
        name = request.title or "Gilbert announcement"

        async def _one(pid: str) -> None:
            client = self._clients.get(pid)
            if client is None:
                return
            try:
                await client.player.play_audio_clip(
                    request.uri,
                    volume=volume,
                    name=name,
                )
            except FailedCommand as exc:
                logger.warning(
                    "Audio clip failed on speaker %s: %s",
                    self._name_for(pid),
                    exc,
                )

        results = await asyncio.gather(
            *(_one(pid) for pid in speaker_ids), return_exceptions=True
        )
        failures = [r for r in results if isinstance(r, Exception)]
        if failures and len(failures) == len(results):
            # Every speaker rejected the clip — surface the first error
            # so the caller knows playback didn't happen.
            raise failures[0]

    async def _ensure_group(self, target_ids: list[str]) -> str:
        """Make sure ``target_ids`` form a single group and return the coordinator.

        Uses the declarative ``set_group_members`` API: if the target
        set matches an existing group, Sonos no-ops; otherwise it
        re-forms the group atomically on the household. Returns the
        player_id of whoever ended up as coordinator.

        Dedupes ``target_ids`` defensively before calling Sonos —
        ``set_group_members`` rejects any list with a repeated
        player_id ("Effective set of new group members has repeated
        player id"), and the callers that feed us from resolved
        speaker-name lists occasionally produce duplicates (e.g.
        when a speaker is addressed by both its device name and an
        alias).
        """
        target_ids = list(dict.fromkeys(target_ids))

        if len(target_ids) == 1:
            # Singleton: make sure the speaker is alone in its group.
            pid = target_ids[0]
            client = self._clients[pid]
            player = client.player
            group = player.group
            if group is None or len(group.player_ids) != 1:
                try:
                    await player.leave_group()
                except FailedCommand:
                    logger.debug(
                        "Sonos leave_group failed for %s — speaker may "
                        "already be solo",
                        pid,
                        exc_info=True,
                    )
            return pid

        # Multi-speaker: pick a coordinator (first target), call
        # set_group_members on its current group with the full target
        # list. Sonos merges + expels as needed.
        coord_id = target_ids[0]
        client = self._clients[coord_id]
        player = client.player
        group = player.group
        if group is None:
            # Edge case: coordinator has no group membership yet.
            # Create one from scratch.
            await client.create_group(target_ids)
            return coord_id

        current = set(group.player_ids)
        wanted = set(target_ids)
        if current == wanted:
            return coord_id
        await group.set_group_members(target_ids)
        return coord_id

    async def _set_group_volume(
        self, speaker_ids: list[str], volume: int
    ) -> None:
        """Apply the same volume to every speaker in ``speaker_ids``."""
        volume = max(0, min(100, int(volume)))

        async def _one(pid: str) -> None:
            client = self._clients.get(pid)
            if client is None:
                return
            try:
                await client.player.set_volume(volume)
            except FailedCommand:
                logger.debug(
                    "set_volume failed for %s", self._name_for(pid), exc_info=True
                )

        await asyncio.gather(*(_one(pid) for pid in speaker_ids))

    async def stop(self, speaker_ids: list[str] | None = None) -> None:
        """Stop playback on the targets (or everyone, if None)."""
        targets = speaker_ids or list(self._player_metadata.keys())
        seen_groups: set[str] = set()
        for pid in targets:
            client = self._clients.get(pid)
            if client is None:
                continue
            group = client.player.group
            if group is None or group.id in seen_groups:
                continue
            seen_groups.add(group.id)
            try:
                await group.pause()
            except FailedCommand:
                logger.debug(
                    "Pause failed for group %s",
                    group.id,
                    exc_info=True,
                )

    # ── Volume ───────────────────────────────────────────────────────

    async def get_volume(self, speaker_id: str) -> int:
        client = self._clients.get(speaker_id)
        if client is None:
            raise KeyError(f"Unknown speaker: {speaker_id}")
        return int(client.player.volume_level or 0)

    async def set_volume(self, speaker_id: str, volume: int) -> None:
        client = self._clients.get(speaker_id)
        if client is None:
            raise KeyError(f"Unknown speaker: {speaker_id}")
        await client.player.set_volume(max(0, min(100, int(volume))))

    # ── Transport state ──────────────────────────────────────────────

    async def get_playback_state(self, speaker_id: str) -> PlaybackState:
        client = self._clients.get(speaker_id)
        if client is None:
            return PlaybackState.STOPPED
        group = client.player.group
        if group is None:
            return PlaybackState.STOPPED
        return _PLAYBACK_STATE_MAP.get(
            str(group.playback_state or ""),
            PlaybackState.STOPPED,
        )

    async def get_now_playing(self, speaker_id: str) -> NowPlaying:
        """Pull the latest metadata for whatever's playing on the speaker's group."""
        client = self._clients.get(speaker_id)
        state = await self.get_playback_state(speaker_id)
        if client is None:
            return NowPlaying(state=state)

        group = client.player.group
        if group is None:
            return NowPlaying(state=state)

        meta = group.playback_metadata
        if meta is None:
            return NowPlaying(state=state)

        # aiosonos's MetadataStatus exposes ``currentItem`` with a
        # ``track`` object, plus the current ``positionMillis`` and
        # queue position. Fields are optional — fall back to empty.
        current_item = getattr(meta, "currentItem", None)
        track = getattr(current_item, "track", None) if current_item else None
        title = str(getattr(track, "name", "") or "") if track else ""
        artist = ""
        if track is not None:
            artist_obj = getattr(track, "artist", None)
            artist = str(getattr(artist_obj, "name", "") or "") if artist_obj else ""
        album = ""
        if track is not None:
            album_obj = getattr(track, "album", None)
            album = str(getattr(album_obj, "name", "") or "") if album_obj else ""
        album_art = ""
        if track is not None:
            images = getattr(track, "images", None) or []
            if images:
                album_art = str(getattr(images[0], "url", "") or "")
        duration_ms = (
            int(getattr(track, "durationMillis", 0) or 0) if track else 0
        )
        position_ms = int(getattr(meta, "positionMillis", 0) or 0)

        return NowPlaying(
            state=state,
            title=title,
            artist=artist,
            album=album,
            album_art_url=album_art,
            duration_seconds=duration_ms / 1000.0,
            position_seconds=position_ms / 1000.0,
        )

    # ── Grouping ─────────────────────────────────────────────────────

    @property
    def supports_grouping(self) -> bool:
        return True

    async def list_groups(self) -> list[SpeakerGroup]:
        """Return every unique group across every household's clients."""
        seen: dict[str, SpeakerGroup] = {}
        for client in self._clients.values():
            for group in client.groups:
                if group.id in seen:
                    continue
                seen[group.id] = SpeakerGroup(
                    group_id=group.id,
                    name=group.name or "",
                    coordinator_id=group.coordinator_id or "",
                    member_ids=list(group.player_ids),
                )
        return sorted(seen.values(), key=lambda g: g.name.lower())

    async def group_speakers(self, speaker_ids: list[str]) -> SpeakerGroup:
        """Form a group from ``speaker_ids``; returns the resulting group."""
        if not speaker_ids:
            raise ValueError("speaker_ids is empty")
        coord_id = await self._ensure_group(speaker_ids)
        group = self._clients[coord_id].player.group
        if group is None:
            raise RuntimeError(
                "Expected coordinator to be in a group after _ensure_group"
            )
        return SpeakerGroup(
            group_id=group.id,
            name=group.name or "",
            coordinator_id=group.coordinator_id or coord_id,
            member_ids=list(group.player_ids),
        )

    async def ungroup_speakers(self, speaker_ids: list[str]) -> None:
        """Split each requested speaker into its own solo group."""
        for pid in speaker_ids:
            client = self._clients.get(pid)
            if client is None:
                continue
            try:
                await client.player.leave_group()
            except FailedCommand:
                logger.debug(
                    "leave_group failed for %s", self._name_for(pid), exc_info=True
                )

    # ── Snapshot/restore — no-op under aiosonos ──────────────────────

    async def snapshot(self, speaker_ids: list[str]) -> None:
        """No-op: the aiosonos ``audio_clip`` API self-restores.

        Callers that still invoke ``snapshot``/``restore`` around an
        announcement flow (notably ``SpeakerService.announce``) don't
        need to change — they just become cheap no-ops. The proper
        integration is to set ``PlayRequest.announce=True``, which
        routes to ``player.play_audio_clip`` and handles duck+restore
        natively.
        """

    async def restore(self, speaker_ids: list[str]) -> None:
        """See ``snapshot``."""

    # ── Helpers ──────────────────────────────────────────────────────

    def _name_for(self, player_id: str) -> str:
        meta = self._player_metadata.get(player_id)
        return meta.name if meta else player_id


# ── Spotify URI parsing ──────────────────────────────────────────────


@dataclass
class _SpotifyRef:
    kind: str  # track | album | playlist | artist | episode | show
    id: str
    uri: str  # canonical ``spotify:<kind>:<id>``


# Spotify content-kind strings accepted by Sonos's ``playback.loadContent``.
# Per docs.sonos.com/docs/playback-objects the ``type`` field uses
# lowercase values (``track``, ``playlist``, ``album``, ``artist``,
# ``episode``, ``show``). The aiosonos docstring shows uppercase — that's
# stale; the local API returns a malformed-request error for uppercase.
_SPOTIFY_KIND_TO_LOAD_TYPE: dict[str, str] = {
    "track": "track",
    "album": "album",
    "playlist": "playlist",
    "artist": "artist",
    "episode": "episode",
    "show": "show",
}


def _extract_spotify_ref(uri: str) -> _SpotifyRef | None:
    """Detect a Spotify reference in ``uri``.

    Accepts both the canonical ``spotify:track:…`` scheme and
    ``https://open.spotify.com/track/…`` web URLs. Returns ``None``
    when the URI is neither — caller should treat as a plain HTTP
    stream.
    """
    if not uri:
        return None
    stripped = uri.strip()
    match = _SPOTIFY_URI_RE.match(stripped)
    if match:
        kind, obj_id = match.group(1), match.group(2)
        return _SpotifyRef(kind=kind, id=obj_id, uri=stripped)
    match = _SPOTIFY_OPEN_URL_RE.search(stripped)
    if match:
        kind, obj_id = match.group(1), match.group(2)
        return _SpotifyRef(
            kind=kind, id=obj_id, uri=f"spotify:{kind}:{obj_id}"
        )
    return None
