"""Tests for the aiosonos-backed Sonos speaker backend.

The backend is a fairly thin adapter over aiosonos's WebSocket API —
most of its job is wiring up zeroconf discovery, keeping a client per
discovered player, and mapping between our SpeakerBackend interface and
aiosonos's SonosPlayer / SonosGroup / audio_clip surfaces.

Tests mock the aiosonos client/player/group objects. We don't spin up
a real WebSocket; the behaviours we care about are:

- Spotify URI / open.spotify.com URL detection.
- ``announce=True`` routes to ``player.play_audio_clip``.
- Plain HTTP URIs route to ``group.play_stream_url``.
- Spotify URIs raise NotImplementedError (deferred to the music backend
  so ``accountId`` resolution lives in one place).
- ``_ensure_group`` uses declarative grouping and is a no-op when
  membership already matches.
- Snapshot/restore are no-ops (aiosonos ``audio_clip`` self-restores).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert_plugin_sonos.sonos_speaker import (
    SonosSpeaker,
    _extract_spotify_ref,
    _PlayerMetadata,
)

from gilbert.interfaces.speaker import PlayRequest


# ── Spotify URI parsing ──────────────────────────────────────────────


def test_extract_spotify_track_uri() -> None:
    ref = _extract_spotify_ref("spotify:track:3w0pyHgJJW9JN0cJxmi33Z")
    assert ref is not None
    assert ref.kind == "track"
    assert ref.id == "3w0pyHgJJW9JN0cJxmi33Z"
    assert ref.uri == "spotify:track:3w0pyHgJJW9JN0cJxmi33Z"


def test_extract_spotify_open_url() -> None:
    """``https://open.spotify.com/playlist/…`` web URLs get canonicalized
    to ``spotify:playlist:…`` so downstream handling doesn't need two
    code paths."""
    ref = _extract_spotify_ref(
        "https://open.spotify.com/playlist/37i9dQZF1DX?si=abc"
    )
    assert ref is not None
    assert ref.kind == "playlist"
    assert ref.uri == "spotify:playlist:37i9dQZF1DX"


def test_extract_spotify_returns_none_for_plain_http() -> None:
    assert (
        _extract_spotify_ref("http://192.168.1.20:8000/api/share/abc") is None
    )


def test_extract_spotify_returns_none_for_empty() -> None:
    assert _extract_spotify_ref("") is None
    assert _extract_spotify_ref("   ") is None


# ── Test scaffolding ─────────────────────────────────────────────────


def _make_backend_with_mock_speaker(
    player_id: str = "RINCON_COORD",
    group_in: MagicMock | None = None,
) -> tuple[SonosSpeaker, MagicMock, MagicMock, MagicMock]:
    """Spin up a SonosSpeaker with one mock aiosonos client.

    Returns ``(backend, client_mock, player_mock, group_mock)``.
    """
    backend = SonosSpeaker()

    group = group_in if group_in is not None else MagicMock()
    if not hasattr(group, "id") or not group.id:
        group.id = "group-1"
    if not hasattr(group, "name") or not group.name:
        group.name = "Kitchen"
    if not hasattr(group, "player_ids"):
        group.player_ids = [player_id]
    if not hasattr(group, "coordinator_id"):
        group.coordinator_id = player_id
    if not hasattr(group, "playback_state"):
        group.playback_state = "PLAYBACK_STATE_IDLE"
    if not hasattr(group, "playback_metadata"):
        group.playback_metadata = None
    if not hasattr(group, "play_stream_url") or not isinstance(
        group.play_stream_url, AsyncMock
    ):
        group.play_stream_url = AsyncMock()
    if not hasattr(group, "pause") or not isinstance(group.pause, AsyncMock):
        group.pause = AsyncMock()
    if not hasattr(group, "set_group_members") or not isinstance(
        group.set_group_members, AsyncMock
    ):
        group.set_group_members = AsyncMock()

    player = MagicMock()
    player.id = player_id
    player.name = "Kitchen"
    player.volume_level = 30
    player.is_coordinator = True
    player.group = group
    player.play_audio_clip = AsyncMock()
    player.set_volume = AsyncMock()
    player.leave_group = AsyncMock()

    client = MagicMock()
    client.player = player
    client.groups = [group]
    client.create_group = AsyncMock()
    client.disconnect = AsyncMock()

    backend._clients[player_id] = client
    backend._player_metadata[player_id] = _PlayerMetadata(
        player_id=player_id,
        household_id="HH-TEST",
        name="Kitchen",
        ip_address="192.168.1.20",
        model="Sonos One",
    )
    return backend, client, player, group


# ── Announce routes to audio_clip ────────────────────────────────────


async def test_announce_uses_audio_clip() -> None:
    """``PlayRequest(announce=True)`` should hand the URL straight to
    ``player.play_audio_clip`` — not go through the group-forming /
    stream-loading path, and not require any snapshot/restore."""
    backend, _client, player, group = _make_backend_with_mock_speaker()

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/api/share/abc.mp3",
            speaker_ids=["RINCON_COORD"],
            announce=True,
            title="Ding",
            volume=40,
        )
    )

    player.play_audio_clip.assert_awaited_once()
    assert (
        player.play_audio_clip.call_args.args[0]
        == "http://gilbert/api/share/abc.mp3"
    )
    assert player.play_audio_clip.call_args.kwargs.get("volume") == 40
    assert player.play_audio_clip.call_args.kwargs.get("name") == "Ding"

    group.play_stream_url.assert_not_awaited()
    group.set_group_members.assert_not_awaited()


async def test_announce_with_multiple_speakers_parallelizes() -> None:
    backend, _, player_a, _ = _make_backend_with_mock_speaker(
        player_id="RINCON_A"
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    client_b = MagicMock()
    player_b = MagicMock()
    player_b.play_audio_clip = AsyncMock()
    client_b.player = player_b
    backend._clients["RINCON_B"] = client_b

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/x.mp3",
            speaker_ids=["RINCON_A", "RINCON_B"],
            announce=True,
        )
    )

    player_a.play_audio_clip.assert_awaited_once()
    player_b.play_audio_clip.assert_awaited_once()


# ── Plain HTTP URIs route to play_stream_url ─────────────────────────


async def test_http_uri_uses_play_stream_url() -> None:
    backend, _client, _player, group = _make_backend_with_mock_speaker()

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/api/share/song.mp3",
            speaker_ids=["RINCON_COORD"],
        )
    )

    group.play_stream_url.assert_awaited_once()
    args, kwargs = group.play_stream_url.call_args
    assert args[0] == "http://gilbert/api/share/song.mp3"
    assert kwargs.get("play_on_completion") is False


async def test_http_uri_applies_volume_before_play() -> None:
    backend, _client, player, group = _make_backend_with_mock_speaker()

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_COORD"],
            volume=55,
        )
    )

    player.set_volume.assert_awaited_once_with(55)
    group.play_stream_url.assert_awaited_once()


async def test_http_uri_clamps_volume_to_valid_range() -> None:
    backend, _client, player, _group = _make_backend_with_mock_speaker()

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_COORD"],
            volume=150,
        )
    )

    player.set_volume.assert_awaited_once_with(100)


# ── Spotify URIs use load_content ────────────────────────────────────


async def test_spotify_uri_uses_load_content() -> None:
    """Spotify URIs go through ``playback.load_content`` with a
    MetadataId pointing at the speaker's linked Spotify account.
    ``accountId`` is intentionally omitted — Sonos resolves the
    default linked Spotify account on the household, which is the
    correct behaviour for single-account setups."""
    backend, client, _player, group = _make_backend_with_mock_speaker()
    client.api = MagicMock()
    client.api.playback = MagicMock()
    client.api.playback.load_content = AsyncMock()

    await backend.play_uri(
        PlayRequest(
            uri="spotify:track:3w0pyHgJJW9JN0cJxmi33Z",
            speaker_ids=["RINCON_COORD"],
        )
    )

    client.api.playback.load_content.assert_awaited_once()
    args = client.api.playback.load_content.call_args.args
    # load_content signature: (group_id, content).
    group_id, content = args
    assert group_id == group.id
    # Payload shape per Sonos docs — lowercase type, Spotify service ID,
    # canonical Spotify URI as objectId.
    assert content["type"] == "track"
    assert content["id"]["serviceId"] == "9"
    assert content["id"]["objectId"] == "spotify:track:3w0pyHgJJW9JN0cJxmi33Z"
    assert content["playbackAction"] == "PLAY"
    # No stream_url call for Spotify content.
    group.play_stream_url.assert_not_awaited()


async def test_spotify_playlist_uses_playlist_type() -> None:
    """``type`` maps to the Spotify content kind — passing a playlist
    URI must produce ``"playlist"`` not ``"track"``. Wrong type =
    Sonos plays nothing."""
    backend, client, _player, _group = _make_backend_with_mock_speaker()
    client.api = MagicMock()
    client.api.playback = MagicMock()
    client.api.playback.load_content = AsyncMock()

    await backend.play_uri(
        PlayRequest(
            uri="spotify:playlist:37i9dQZF1DX",
            speaker_ids=["RINCON_COORD"],
        )
    )

    content = client.api.playback.load_content.call_args.args[1]
    assert content["type"] == "playlist"
    assert content["id"]["objectId"] == "spotify:playlist:37i9dQZF1DX"


# ── Declarative grouping ─────────────────────────────────────────────


async def test_ensure_group_noop_when_membership_matches() -> None:
    """If the coordinator's group already contains exactly the target
    members, ``set_group_members`` shouldn't be called — avoiding an
    unnecessary WebSocket round-trip that would briefly drop playback."""
    group = MagicMock()
    group.id = "group-1"
    group.name = "Zone"
    group.player_ids = ["RINCON_A", "RINCON_B"]
    group.coordinator_id = "RINCON_A"
    group.playback_state = "PLAYBACK_STATE_IDLE"
    group.set_group_members = AsyncMock()
    group.play_stream_url = AsyncMock()

    backend, _client, _player, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A",
        group_in=group,
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_player = MagicMock()
    b_player.group = group
    b_client = MagicMock()
    b_client.player = b_player
    backend._clients["RINCON_B"] = b_client

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_A", "RINCON_B"],
        )
    )

    group.set_group_members.assert_not_awaited()
    group.play_stream_url.assert_awaited_once()


async def test_ensure_group_reforms_when_membership_differs() -> None:
    """If the target set doesn't match the coordinator's current group,
    ``set_group_members`` gets called once with the desired list."""
    group = MagicMock()
    group.id = "group-1"
    group.player_ids = ["RINCON_A"]
    group.coordinator_id = "RINCON_A"
    group.playback_state = "PLAYBACK_STATE_IDLE"
    group.set_group_members = AsyncMock()
    group.play_stream_url = AsyncMock()

    backend, _c, _p, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A",
        group_in=group,
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_client = MagicMock()
    b_client.player = MagicMock(group=group)
    backend._clients["RINCON_B"] = b_client

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_A", "RINCON_B"],
        )
    )

    group.set_group_members.assert_awaited_once_with(["RINCON_A", "RINCON_B"])


# ── Snapshot/restore are no-ops ──────────────────────────────────────


async def test_snapshot_and_restore_are_noops() -> None:
    """Snapshot/restore are kept on the interface for backward compat
    but aiosonos's ``audio_clip`` self-restores — the new backend
    doesn't need them. Just verify they don't raise and don't poke the
    client's mutating methods."""
    backend, _client, player, group = _make_backend_with_mock_speaker()
    await backend.snapshot(["RINCON_COORD"])
    await backend.restore(["RINCON_COORD"])
    player.set_volume.assert_not_called()
    group.play_stream_url.assert_not_called()


# ── Volume ───────────────────────────────────────────────────────────


async def test_set_volume() -> None:
    backend, _client, player, _group = _make_backend_with_mock_speaker()
    await backend.set_volume("RINCON_COORD", 42)
    player.set_volume.assert_awaited_once_with(42)


async def test_set_volume_unknown_speaker_raises() -> None:
    backend, *_ = _make_backend_with_mock_speaker()
    with pytest.raises(KeyError, match="Unknown speaker"):
        await backend.set_volume("RINCON_NOPE", 42)


async def test_get_volume() -> None:
    backend, _client, player, _group = _make_backend_with_mock_speaker()
    player.volume_level = 73
    assert await backend.get_volume("RINCON_COORD") == 73


# ── Stop ─────────────────────────────────────────────────────────────


async def test_stop_dedupes_across_group_members() -> None:
    """When multiple speakers share a group, ``stop`` pauses the group
    once — pausing the same group N times is wasteful and can race."""
    group = MagicMock()
    group.id = "group-1"
    group.pause = AsyncMock()

    backend, _c, _p, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A", group_in=group
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_client = MagicMock()
    b_client.player = MagicMock(group=group)
    backend._clients["RINCON_B"] = b_client

    await backend.stop(["RINCON_A", "RINCON_B"])

    group.pause.assert_awaited_once()


# ── Feature flags ────────────────────────────────────────────────────


def test_supports_grouping_is_true() -> None:
    assert SonosSpeaker().supports_grouping is True


def test_backend_name() -> None:
    assert SonosSpeaker.backend_name == "sonos"


# ── list_speakers materialization ────────────────────────────────────


async def test_list_speakers_reflects_live_state() -> None:
    """list_speakers should pull the current volume + group from the
    client, not a cached snapshot taken at discovery time — otherwise
    a volume change via the Sonos app wouldn't show up until
    reconnection."""
    backend, _c, player, group = _make_backend_with_mock_speaker()
    player.volume_level = 85
    group.name = "Living Zone"
    group.id = "live-group-id"
    group.playback_state = "PLAYBACK_STATE_PLAYING"

    infos = await backend.list_speakers()
    assert len(infos) == 1
    info = infos[0]
    assert info.volume == 85
    assert info.group_name == "Living Zone"
    assert info.group_id == "live-group-id"
    assert info.state.value == "playing"
