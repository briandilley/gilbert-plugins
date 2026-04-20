"""Sonos speaker backend — speaker control via the SoCo library."""

import asyncio
import html
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
import soco
from soco import SoCo

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

# Grouping timing constants
_GROUP_SETTLE_SECONDS = 2.0
_GROUP_POLL_INTERVAL = 1.0
_GROUP_POLL_TIMEOUT = 5.0  # seconds to poll before retrying group command
_GROUP_MAX_ATTEMPTS = 5  # max times to retry the whole group operation

# Map SoCo transport states to our enum
_STATE_MAP: dict[str, PlaybackState] = {
    "PLAYING": PlaybackState.PLAYING,
    "PAUSED_PLAYBACK": PlaybackState.PAUSED,
    "STOPPED": PlaybackState.STOPPED,
    "TRANSITIONING": PlaybackState.TRANSITIONING,
}


def _parse_hms(value: str) -> float:
    """Parse a Sonos-style duration/position string (``H:MM:SS``) into seconds.

    Returns 0.0 for empty, ``NOT_IMPLEMENTED``, or malformed values.
    """
    if not value or value == "NOT_IMPLEMENTED":
        return 0.0
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return 0.0


def _speaker_id(device: SoCo) -> str:
    """Canonical speaker ID — use the UID which is stable."""
    return device.uid


def _speaker_info(device: SoCo) -> SpeakerInfo:
    """Build a SpeakerInfo from a SoCo device.

    During Sonos topology changes (e.g. just after an ``unjoin``), a
    device's ``group`` can exist while its ``coordinator`` is
    temporarily ``None``. Treat those devices as their own coordinator
    until the group state settles — otherwise ``coordinator.uid``
    raises ``AttributeError`` on a ``None`` object.
    """
    group = device.group
    if group is not None and group.coordinator is not None:
        coordinator = group.coordinator
    else:
        coordinator = device
    transport = device.get_current_transport_info()
    state_str = transport.get("current_transport_state", "STOPPED")

    return SpeakerInfo(
        speaker_id=_speaker_id(device),
        name=device.player_name,
        ip_address=device.ip_address,
        model=device.get_speaker_info().get("model_name", ""),
        group_id=group.uid if group else "",
        group_name=group.label if group else "",
        is_group_coordinator=(device.uid == coordinator.uid),
        volume=device.volume,
        state=_STATE_MAP.get(state_str, PlaybackState.STOPPED),
    )


def _find_device(devices: dict[str, SoCo], speaker_id: str) -> SoCo:
    """Find a device by speaker_id. Raises KeyError if not found."""
    device = devices.get(speaker_id)
    if device is None:
        raise KeyError(f"Speaker not found: {speaker_id}")
    return device


def _spotify_url_to_uri(url: str) -> str:
    """Convert a Spotify web URL to a ``spotify:`` URI.

    ``https://open.spotify.com/track/abc123?si=xyz`` → ``spotify:track:abc123``
    ``https://open.spotify.com/playlist/def456`` → ``spotify:playlist:def456``
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    # Path is like /track/abc123 or /playlist/def456
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2:
        resource_type = parts[0]  # track, playlist, album, etc.
        resource_id = parts[1]
        return f"spotify:{resource_type}:{resource_id}"
    return url  # Return original if we can't parse it


def _detect_spotify_sn(devices: dict[str, SoCo]) -> int:
    """Detect the Spotify account serial number from the Sonos system.

    Tries multiple sources: the accounts API, currently-playing tracks,
    and Sonos favorites.
    """
    import re

    from soco.music_services.accounts import Account

    def _extract_sn(uri: str) -> int | None:
        if "spotify" in uri:
            m = re.search(r"sn=(\d+)", uri)
            if m:
                return int(m.group(1))
        return None

    # Method 1: accounts API
    for acct in Account.get_accounts().values():
        if acct.service_type == 3079:
            return int(acct.serial_number)

    for speaker in devices.values():
        # Method 2: currently-playing track
        try:
            track = speaker.get_current_track_info()
            sn = _extract_sn(track.get("uri", ""))
            if sn is not None:
                return sn
        except Exception:
            pass

        # Method 3: Sonos favorites
        try:
            favs = speaker.music_library.get_sonos_favorites()
            for fav in favs:
                res = fav.resources[0] if fav.resources else None
                if res:
                    sn = _extract_sn(res.uri)
                    if sn is not None:
                        return sn
        except Exception:
            pass

    return 0


def _to_sonos_spotify_uri(spotify_uri: str, sn: int = 0) -> str:
    """Convert a ``spotify:track:ID`` URI to Sonos ``x-sonos-spotify:`` format.

    Builds the URI with the correct service ID, flags, and serial number
    for the local Sonos system's linked Spotify account.  SoCo's
    ``play_uri`` will generate appropriate metadata from the title arg.

    Only valid for *single items* (tracks, episodes, shows). Spotify
    containers (playlists, albums, artists) must be routed through
    ``_play_container`` instead — see ``sonos_music._build_spotify_container_playable``
    for the upstream construction and ``_play_container`` for the
    AVTransport queue-playback path.
    """
    from soco.music_services import MusicService

    svc = MusicService("Spotify")
    encoded = spotify_uri.replace(":", "%3a")
    return f"x-sonos-spotify:{encoded}?sid={svc.service_id}&flags=8232&sn={sn}"


def _play_container(coordinator: SoCo, uri: str, didl: str) -> None:
    """Play a container URI (Spotify playlist/album/artist) via the queue.

    Clears the current queue, enqueues the container with its DIDL-Lite
    envelope, and plays from the first newly-enqueued track. This is the
    *only* path that works for Spotify containers: handing a container
    URI to ``SetAVTransportURI`` silently no-ops — the transport accepts
    the call and Sonos logs it as successful, but no audio plays because
    the container URI without queue context gives Sonos no track to
    start on. ``AddURIToQueue`` with the proper container DIDL tells
    Sonos to expand the container into individual tracks on the queue,
    and ``play_from_queue`` then plays the first one.
    """
    if not didl:
        raise ValueError(
            f"Container URI requires DIDL metadata; got empty didl_meta for {uri}",
        )
    coordinator.clear_queue()
    response = coordinator.avTransport.AddURIToQueue(
        [
            ("InstanceID", 0),
            ("EnqueuedURI", uri),
            ("EnqueuedURIMetaData", didl),
            ("DesiredFirstTrackNumberEnqueued", 0),
            ("EnqueueAsNext", 0),
        ],
    )
    first = int(response.get("FirstTrackNumberEnqueued", "1"))
    # FirstTrackNumberEnqueued is 1-based; play_from_queue takes 0-based.
    coordinator.play_from_queue(first - 1)


async def _probe_http_audio_mime(url: str, timeout: float = 5.0) -> str:
    """HEAD-probe an HTTP(S) URL and return its audio Content-Type.

    Returns ``""`` when the URL isn't HTTP(S), the probe fails, the
    server rejects HEAD, or the Content-Type isn't ``audio/*`` /
    ``video/*``. In any of those cases the caller should fall back to
    soco's default (radio-style) DIDL — probing is a best-effort
    improvement, not a hard requirement. Timeout is short so a dead
    URL doesn't stall the whole play request.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True
        ) as client:
            resp = await client.head(url)
    except Exception:
        return ""
    if resp.status_code >= 400:
        return ""
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype.startswith(("audio/", "video/")):
        return ctype
    return ""


def _build_music_track_didl(uri: str, title: str, mime: str) -> str:
    """Build a DIDL-Lite envelope that tags ``uri`` as a finite music track.

    soco's default auto-generated DIDL (when we hand it a title but no
    ``meta``) marks the resource as ``object.item.audioItem.audioBroadcast``
    — a live radio stream. Sonos then probes the URL and rejects plain
    HTTP file responses with UPnP error 714 "Illegal MIME-Type" because
    the content doesn't look like a radio stream to its whitelist.

    Emitting the ``musicTrack`` class with an explicit ``protocolInfo``
    carrying the real MIME tells Sonos "this is a regular audio file",
    skipping the radio-stream MIME check. Works for one-off playback of
    arbitrary HTTP-hosted audio, which is exactly the
    ``share_workspace_file``-style flow.
    """
    safe_title = html.escape(title or "Gilbert audio")
    safe_uri = html.escape(uri, quote=True)
    safe_mime = html.escape(mime, quote=True)
    return (
        '<DIDL-Lite '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="gilbert-share" parentID="-1" restricted="true">'
        f"<dc:title>{safe_title}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:{safe_mime}:*">{safe_uri}</res>'
        "</item></DIDL-Lite>"
    )


class SonosSpeaker(SpeakerBackend):
    """Sonos speaker backend using the SoCo library."""

    backend_name = "sonos"

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Run Sonos discovery and report how many speakers are reachable on the network."
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
        try:
            found = await asyncio.to_thread(soco.discover)
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Sonos discovery failed: {exc}",
            )
        devices = list(found) if found else []
        if not devices:
            return ConfigActionResult(
                status="error",
                message="No Sonos speakers found on the network.",
            )
        # Refresh the cached device map so subsequent calls see them.
        self._devices = {_speaker_id(d): d for d in devices}
        return ConfigActionResult(
            status="ok",
            message=f"Found {len(devices)} Sonos speaker(s) on the network.",
        )

    def __init__(self) -> None:
        self._devices: dict[str, SoCo] = {}
        self._spotify_sn: int = 0
        self._snapshots: dict[str, Any] = {}

    async def initialize(self, config: dict[str, object]) -> None:
        await self._discover()
        self._spotify_sn = await asyncio.to_thread(_detect_spotify_sn, self._devices)
        logger.info(
            "Sonos backend initialized — %d speakers found (spotify sn=%d)",
            len(self._devices),
            self._spotify_sn,
        )

    async def close(self) -> None:
        self._devices.clear()

    # --- Discovery ---

    async def _discover(self) -> None:
        """Discover Sonos speakers on the network."""
        devices = await asyncio.to_thread(soco.discover)
        self._devices = {}
        if devices:
            for device in devices:
                self._devices[_speaker_id(device)] = device

    async def list_speakers(self) -> list[SpeakerInfo]:
        await self._discover()
        return await asyncio.to_thread(self._list_speakers_sync)

    def _list_speakers_sync(self) -> list[SpeakerInfo]:
        return [_speaker_info(d) for d in self._devices.values()]

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        device = self._devices.get(speaker_id)
        if device is None:
            await self._discover()
            device = self._devices.get(speaker_id)
        if device is None:
            return None
        return await asyncio.to_thread(_speaker_info, device)

    # --- Playback ---

    async def play_uri(self, request: PlayRequest) -> None:
        target_ids = request.speaker_ids or list(self._devices.keys())
        if not target_ids:
            raise ValueError("No speakers available")

        # Find the coordinator — after topology is prepared by the speaker
        # service, the first target is standalone or the group coordinator.
        coordinator = await self._find_coordinator(target_ids)

        # Set volume if requested
        if request.volume is not None:
            for sid in target_ids:
                dev = self._devices.get(sid)
                if dev:
                    await asyncio.to_thread(setattr, dev, "volume", request.volume)

        # Play — Spotify URIs need conversion to Sonos format
        title = request.title or ""
        uri = request.uri

        # Convert Spotify web URLs to spotify: URIs
        # e.g. https://open.spotify.com/track/abc123 → spotify:track:abc123
        if "open.spotify.com/" in uri:
            uri = _spotify_url_to_uri(uri)

        # Container URIs (Spotify playlists/albums/artists from the music
        # backend's resolve_playable) must be enqueued, not handed to
        # SetAVTransportURI directly — Sonos silently accepts the direct
        # call and plays nothing. Route them through the queue path.
        if uri.startswith("x-rincon-cpcontainer:"):
            try:
                await asyncio.to_thread(
                    _play_container,
                    coordinator,
                    uri,
                    request.didl_meta,
                )
            except Exception:
                logger.exception(
                    "Sonos container playback failed: uri=%s speaker=%s",
                    uri,
                    coordinator.player_name,
                )
                raise
        else:
            if uri.startswith("spotify:"):
                uri = await asyncio.to_thread(
                    _to_sonos_spotify_uri,
                    uri,
                    self._spotify_sn,
                )
            try:
                if request.didl_meta:
                    await asyncio.to_thread(
                        coordinator.play_uri,
                        uri,
                        meta=request.didl_meta,
                        title=title,
                    )
                else:
                    # soco.play_uri(uri, title=X) with no meta generates a
                    # radio-stream DIDL (class ``audioItem.audioBroadcast``)
                    # — Sonos then rejects plain HTTP file URLs with UPnP
                    # error 714 because the content MIME doesn't match its
                    # radio-stream whitelist. Probe the URL ourselves;
                    # when it's a regular audio file, emit a music-track
                    # DIDL with an explicit ``protocolInfo`` so Sonos
                    # treats it as a finite file, not a live stream.
                    probed_mime = await _probe_http_audio_mime(uri)
                    if probed_mime:
                        didl = _build_music_track_didl(uri, title, probed_mime)
                        await asyncio.to_thread(
                            coordinator.play_uri,
                            uri,
                            meta=didl,
                            title=title,
                        )
                    else:
                        await asyncio.to_thread(
                            coordinator.play_uri, uri, title=title
                        )
            except Exception:
                logger.exception(
                    "Sonos play_uri failed: uri=%s speaker=%s",
                    uri,
                    coordinator.player_name,
                )
                raise

        # Seek to position if requested
        if request.position_seconds is not None and request.position_seconds > 0:
            pos = int(request.position_seconds)
            timestamp = f"{pos // 3600}:{(pos % 3600) // 60:02d}:{pos % 60:02d}"
            await asyncio.sleep(0.3)  # brief pause for transport to start
            await asyncio.to_thread(coordinator.seek, timestamp)

        logger.info("Playing %s on %s", request.uri, coordinator.player_name)

    async def _find_coordinator(self, target_ids: list[str]) -> SoCo:
        """Find the group coordinator for the given speakers.

        If the speakers are grouped, returns the coordinator. If a single
        standalone speaker, returns that device.
        """
        device = _find_device(self._devices, target_ids[0])
        group = await asyncio.to_thread(lambda: device.group)
        return group.coordinator if group else device

    async def clear_queue(self, speaker_ids: list[str] | None = None) -> None:
        """Clear the playback queue on the specified speakers."""
        targets = speaker_ids or list(self._devices.keys())
        for sid in targets:
            device = self._devices.get(sid)
            if device:
                try:
                    await asyncio.to_thread(device.clear_queue)
                except Exception:
                    logger.debug("Failed to clear queue on %s", sid)

    async def stop(self, speaker_ids: list[str] | None = None) -> None:
        targets = speaker_ids or list(self._devices.keys())
        for sid in targets:
            device = self._devices.get(sid)
            if device:
                await asyncio.to_thread(device.stop)

    # --- Snapshot / Restore ---

    async def snapshot(self, speaker_ids: list[str]) -> None:
        """Save playback state of speakers so it can be restored after an announcement."""
        from soco.snapshot import Snapshot

        self._snapshots = {}
        for sid in speaker_ids:
            device = self._devices.get(sid)
            if device:
                try:
                    snap = Snapshot(device)
                    await asyncio.to_thread(snap.snapshot)
                    self._snapshots[sid] = snap
                except Exception:
                    logger.debug("Failed to snapshot speaker %s", sid)

    async def restore(self, speaker_ids: list[str]) -> None:
        """Restore speakers to the state saved by snapshot()."""
        for sid in speaker_ids:
            snap = self._snapshots.pop(sid, None)
            if snap:
                try:
                    await asyncio.to_thread(snap.restore)
                except Exception:
                    logger.debug("Failed to restore speaker %s", sid)

    # --- Volume ---

    async def get_playback_state(self, speaker_id: str) -> PlaybackState:
        device = _find_device(self._devices, speaker_id)
        transport = await asyncio.to_thread(device.get_current_transport_info)
        state_str = transport.get("current_transport_state", "STOPPED")
        return _STATE_MAP.get(state_str, PlaybackState.STOPPED)

    async def get_now_playing(self, speaker_id: str) -> NowPlaying:
        """Query Sonos for the currently playing track on a speaker.

        Follows the group coordinator when the speaker is grouped — only the
        coordinator has authoritative transport/track state.
        """
        device = _find_device(self._devices, speaker_id)

        def _fetch() -> NowPlaying:
            # Follow the group coordinator: in a Sonos group, only the
            # coordinator reports the actual track being played.
            group = device.group
            coordinator = group.coordinator if group and group.coordinator else device
            transport = coordinator.get_current_transport_info()
            state = _STATE_MAP.get(
                transport.get("current_transport_state", "STOPPED"),
                PlaybackState.STOPPED,
            )
            try:
                track = coordinator.get_current_track_info()
            except Exception:
                return NowPlaying(state=state)
            return NowPlaying(
                state=state,
                title=track.get("title", "") or "",
                artist=track.get("artist", "") or "",
                album=track.get("album", "") or "",
                album_art_url=track.get("album_art", "") or "",
                uri=track.get("uri", "") or "",
                duration_seconds=_parse_hms(track.get("duration", "")),
                position_seconds=_parse_hms(track.get("position", "")),
            )

        return await asyncio.to_thread(_fetch)

    async def get_volume(self, speaker_id: str) -> int:
        device = _find_device(self._devices, speaker_id)
        return await asyncio.to_thread(lambda: device.volume)

    async def set_volume(self, speaker_id: str, volume: int) -> None:
        device = _find_device(self._devices, speaker_id)
        clamped = max(0, min(100, volume))
        await asyncio.to_thread(setattr, device, "volume", clamped)

    # --- Grouping ---

    @property
    def supports_grouping(self) -> bool:
        return True

    async def list_groups(self) -> list[SpeakerGroup]:
        await self._discover()
        return await asyncio.to_thread(self._list_groups_sync)

    def _list_groups_sync(self) -> list[SpeakerGroup]:
        seen_group_ids: set[str] = set()
        groups: list[SpeakerGroup] = []
        for device in self._devices.values():
            group = device.group
            if group is None or group.uid in seen_group_ids:
                continue
            seen_group_ids.add(group.uid)
            groups.append(
                SpeakerGroup(
                    group_id=group.uid,
                    name=group.label,
                    coordinator_id=_speaker_id(group.coordinator),
                    member_ids=[_speaker_id(m) for m in group.members],
                )
            )
        return groups

    async def group_speakers(self, speaker_ids: list[str]) -> SpeakerGroup:
        if len(speaker_ids) < 2:
            raise ValueError("Need at least 2 speakers to form a group")

        target_set = set(speaker_ids)

        # Check if already correctly grouped
        result = await self._check_group_state(speaker_ids)
        if result:
            logger.info(
                "Speakers already grouped as '%s' — no changes needed",
                result.name,
            )
            return result

        # Retry loop: attempt to form the group, poll until formed,
        # re-attempt if it doesn't converge within the poll timeout
        for attempt in range(1, _GROUP_MAX_ATTEMPTS + 1):
            await self._apply_group_changes(speaker_ids, target_set)

            # Poll until the group is formed or timeout
            result = await self._poll_until_grouped(
                speaker_ids,
                _GROUP_POLL_TIMEOUT,
            )
            if result:
                logger.info(
                    "Speaker group formed: '%s' with %d members",
                    result.name,
                    len(result.member_ids),
                )
                return result

            logger.warning(
                "Group not formed after attempt %d/%d — retrying",
                attempt,
                _GROUP_MAX_ATTEMPTS,
            )

        raise RuntimeError(
            f"Failed to form speaker group after {_GROUP_MAX_ATTEMPTS} "
            f"attempts with {len(speaker_ids)} speakers"
        )

    async def ungroup_speakers(self, speaker_ids: list[str]) -> None:
        changed = False
        for sid in speaker_ids:
            device = self._devices.get(sid)
            if not device:
                continue
            group = await asyncio.to_thread(lambda d=device: d.group)
            if group and len(group.members) > 1:
                await asyncio.to_thread(device.unjoin)
                changed = True
        if changed:
            await asyncio.sleep(_GROUP_SETTLE_SECONDS)
            logger.info("Ungrouped %d speakers", len(speaker_ids))

    # --- Private grouping helpers ---

    async def _check_group_state(
        self,
        speaker_ids: list[str],
    ) -> SpeakerGroup | None:
        """Check if the target speakers are already in the correct group.

        Returns the SpeakerGroup if all target speakers are in the same
        group with no extra members. Returns None otherwise.
        """
        target_set = set(speaker_ids)

        def check() -> SpeakerGroup | None:
            first = self._devices.get(speaker_ids[0])
            if first is None:
                return None
            group = first.group
            if group is None:
                return None
            group_uids = {_speaker_id(m) for m in group.members}
            if group_uids == target_set:
                return SpeakerGroup(
                    group_id=group.uid,
                    name=group.label,
                    coordinator_id=_speaker_id(group.coordinator),
                    member_ids=[_speaker_id(m) for m in group.members],
                )
            return None

        return await asyncio.to_thread(check)

    async def _apply_group_changes(
        self,
        speaker_ids: list[str],
        target_set: set[str],
    ) -> None:
        """Apply the minimal changes to form the desired group.

        Figures out which speakers need to be unjoined from other groups
        and which need to join the coordinator. Avoids touching speakers
        that are already correct.
        """
        coordinator = _find_device(self._devices, speaker_ids[0])

        def compute_changes() -> tuple[list[SoCo], list[SoCo]]:
            """Returns (to_unjoin, to_join) lists."""
            to_unjoin: list[SoCo] = []
            to_join: list[SoCo] = []
            coord_group = coordinator.group
            coord_group_uids = (
                {_speaker_id(m) for m in coord_group.members}
                if coord_group
                else {_speaker_id(coordinator)}
            )

            for sid in speaker_ids:
                device = self._devices.get(sid)
                if device is None:
                    continue
                if device is coordinator:
                    # Coordinator: unjoin if it's in a group with
                    # non-target members
                    if coord_group and len(coord_group.members) > 1:
                        extras = coord_group_uids - target_set
                        if extras:
                            # Unjoin the extras, not the coordinator
                            for m in coord_group.members:
                                mid = _speaker_id(m)
                                if mid in extras:
                                    to_unjoin.append(m)
                    continue
                # Non-coordinator: check if already in coordinator's group
                if _speaker_id(device) in coord_group_uids:
                    continue
                # Needs to leave its current group and join ours
                dev_group = device.group
                if dev_group and len(dev_group.members) > 1:
                    to_unjoin.append(device)
                to_join.append(device)

            return to_unjoin, to_join

        to_unjoin, to_join = await asyncio.to_thread(compute_changes)

        # Unjoin speakers that are in wrong groups. One speaker refusing
        # to leave its current group (rare, but happens when the speaker
        # is mid-stream from a line-in source or in a weird transport
        # state) shouldn't abort the entire operation — log which ones
        # failed and press on with the rest.
        if to_unjoin:
            unjoin_results = await asyncio.gather(
                *(asyncio.to_thread(d.unjoin) for d in to_unjoin),
                return_exceptions=True,
            )
            unjoin_failed = [
                (d, r) for d, r in zip(to_unjoin, unjoin_results, strict=True)
                if isinstance(r, Exception)
            ]
            for device, exc in unjoin_failed:
                logger.warning(
                    "Sonos speaker '%s' refused to unjoin its current group "
                    "(%s) — continuing without it",
                    getattr(device, "player_name", "?"),
                    exc,
                )
            await asyncio.sleep(_GROUP_SETTLE_SECONDS)
            logger.debug(
                "Unjoined %d/%d speakers from other groups",
                len(to_unjoin) - len(unjoin_failed),
                len(to_unjoin),
            )

        # Join speakers to the coordinator. Same tolerance rule — a
        # single speaker throwing UPnP 800 ("Transition not available",
        # usually because it's in a non-idle state) shouldn't kill
        # playback on all the other speakers. Fail only if *every*
        # intended join failed and nothing else got un-grouped into
        # the target zone either; in that case there's no group to
        # play on and the caller deserves the original error.
        if to_join:
            join_results = await asyncio.gather(
                *(asyncio.to_thread(d.join, coordinator) for d in to_join),
                return_exceptions=True,
            )
            paired = list(zip(to_join, join_results, strict=True))
            join_failed = [(d, r) for d, r in paired if isinstance(r, Exception)]
            join_ok = [d for d, r in paired if not isinstance(r, Exception)]
            for device, exc in join_failed:
                logger.warning(
                    "Sonos speaker '%s' refused to join %s's group (%s) — "
                    "continuing without it",
                    getattr(device, "player_name", "?"),
                    coordinator.player_name,
                    exc,
                )
            if join_failed and not join_ok:
                # Every intended join failed; propagate the first error
                # so the AI tool surfaces a real diagnostic rather than
                # silently "succeeding" with an empty group.
                raise join_failed[0][1]
            await asyncio.sleep(_GROUP_SETTLE_SECONDS)
            logger.debug(
                "Joined %d/%d speakers to coordinator",
                len(join_ok),
                len(to_join),
            )

    async def _poll_until_grouped(
        self,
        speaker_ids: list[str],
        timeout: float,
    ) -> SpeakerGroup | None:
        """Poll until the target speakers are in the correct group.

        Returns the SpeakerGroup if formed within timeout, None otherwise.
        """
        elapsed = 0.0
        while elapsed < timeout:
            await self._discover()
            result = await self._check_group_state(speaker_ids)
            if result:
                return result
            await asyncio.sleep(_GROUP_POLL_INTERVAL)
            elapsed += _GROUP_POLL_INTERVAL
        return None
