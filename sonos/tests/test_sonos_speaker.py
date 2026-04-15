"""Tests for the Sonos speaker backend — focused on the tricky bits."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from gilbert_plugin_sonos.sonos_speaker import (
    SonosSpeaker,
    _parse_hms,
    _play_container,
    _speaker_info,
)

from gilbert.interfaces.speaker import PlaybackState, PlayRequest


def _make_device(
    *,
    uid: str = "RINCON_AAA",
    player_name: str = "Kitchen",
    ip_address: str = "192.168.1.10",
    group: Any = None,
    transport_state: str = "STOPPED",
    model: str = "Sonos One",
    volume: int = 30,
) -> Any:
    """Build a SoCo-shaped mock device with the attributes _speaker_info reads."""
    device = SimpleNamespace()
    device.uid = uid
    device.player_name = player_name
    device.ip_address = ip_address
    device.group = group
    device.volume = volume
    device.get_current_transport_info = lambda: {
        "current_transport_state": transport_state,
    }
    device.get_speaker_info = lambda: {"model_name": model}
    return device


def _make_group(
    uid: str = "RINCON_GRP",
    label: str = "Living Zone",
    coordinator: Any = None,
) -> Any:
    group = SimpleNamespace()
    group.uid = uid
    group.label = label
    group.coordinator = coordinator
    return group


def test_speaker_info_with_normal_group() -> None:
    """Standard case: device is in a group with a valid coordinator."""
    coordinator = _make_device(uid="RINCON_CCC", player_name="Living Room")
    group = _make_group(coordinator=coordinator)

    device = _make_device(uid="RINCON_AAA", player_name="Kitchen", group=group)
    info = _speaker_info(device)

    assert info.speaker_id == "RINCON_AAA"
    assert info.name == "Kitchen"
    assert info.is_group_coordinator is False
    assert info.group_id == "RINCON_GRP"
    assert info.group_name == "Living Zone"
    assert info.state == PlaybackState.STOPPED


def test_speaker_info_coordinator_is_self() -> None:
    """Coordinator is the device itself — is_group_coordinator is True."""
    device = _make_device(uid="RINCON_XYZ", player_name="Office")
    group = _make_group(coordinator=device)
    device.group = group

    info = _speaker_info(device)
    assert info.is_group_coordinator is True


def test_speaker_info_with_no_group() -> None:
    """Standalone speaker (no group) — falls back to self as coordinator."""
    device = _make_device(uid="RINCON_AAA", player_name="Kitchen", group=None)
    info = _speaker_info(device)

    assert info.group_id == ""
    assert info.group_name == ""
    assert info.is_group_coordinator is True


def test_parse_hms_standard_format() -> None:
    """SoCo returns positions/durations as ``H:MM:SS``."""
    assert _parse_hms("0:00:00") == 0.0
    assert _parse_hms("0:01:23") == 83.0
    assert _parse_hms("1:02:03") == 3723.0


def test_parse_hms_empty_and_sentinels() -> None:
    """Empty strings and SoCo's NOT_IMPLEMENTED sentinel return 0.0."""
    assert _parse_hms("") == 0.0
    assert _parse_hms("NOT_IMPLEMENTED") == 0.0


def test_parse_hms_malformed_returns_zero() -> None:
    """Malformed strings degrade gracefully to 0.0 rather than raising."""
    assert _parse_hms("garbage") == 0.0
    assert _parse_hms("a:b:c") == 0.0


def test_speaker_info_with_none_coordinator_does_not_crash() -> None:
    """Regression: during Sonos topology changes (e.g. just after an
    unjoin), ``group.coordinator`` can be transiently None. Before the
    fix this raised AttributeError on ``None.uid`` — now it gracefully
    falls back to the device itself as its own coordinator."""
    device = _make_device(uid="RINCON_AAA", player_name="Bedroom")
    group = _make_group(coordinator=None)  # transient None
    device.group = group

    # Previously: AttributeError: 'NoneType' object has no attribute 'uid'
    info = _speaker_info(device)

    # Fallback: treat the device as its own coordinator
    assert info.is_group_coordinator is True
    # Group metadata from the group object is still surfaced
    assert info.group_id == "RINCON_GRP"
    assert info.group_name == "Living Zone"


# ── Container playback (Spotify playlist/album/artist) ──────────────

# Before this fix, the speaker backend sent every Spotify item — track
# OR playlist — down the same ``coordinator.play_uri`` path. That works
# for tracks but silently no-ops for containers: Sonos accepts the SOAP
# call and logs "Playing spotify:playlist:… on Lobby" with no exception,
# but no audio plays because a container URI without queue context gives
# the player no track to start on. The fix dispatches
# ``x-rincon-cpcontainer:`` URIs through the queue path (clear_queue →
# AddURIToQueue → play_from_queue). These tests lock in that dispatch.


def _make_avtransport_mock() -> MagicMock:
    """A SoCo AVTransport service mock whose AddURIToQueue returns the
    same dict shape soco itself sees from real hardware."""
    av = MagicMock()
    av.AddURIToQueue.return_value = {
        "FirstTrackNumberEnqueued": "1",
        "NumTracksAdded": "50",
        "NewQueueLength": "50",
    }
    return av


def _make_coordinator(uid: str = "RINCON_CCC") -> MagicMock:
    coordinator = MagicMock()
    coordinator.uid = uid
    coordinator.player_name = "Living Room"
    coordinator.avTransport = _make_avtransport_mock()
    return coordinator


def test_play_container_clears_queue_and_plays_from_first_enqueued() -> None:
    """The happy path: clear, enqueue, play_from_queue with the right index."""
    coordinator = _make_coordinator()
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        "<item><upnp:class>object.container.playlistContainer</upnp:class></item>"
        "</DIDL-Lite>"
    )
    uri = "x-rincon-cpcontainer:0fffffffspotify%3Aplaylist%3AabcDEF"

    _play_container(coordinator, uri, didl)

    # Queue cleared first — otherwise the playlist appends after whatever's
    # already there and the radio DJ starts on the wrong track.
    coordinator.clear_queue.assert_called_once_with()

    # AddURIToQueue got the URI *and* the DIDL blob. The DIDL is what
    # tells Sonos "this is a container, expand into tracks" — without it
    # the playlist silently no-ops even with the cpcontainer URI.
    coordinator.avTransport.AddURIToQueue.assert_called_once()
    args = coordinator.avTransport.AddURIToQueue.call_args[0][0]
    arg_dict = dict(args)
    assert arg_dict["EnqueuedURI"] == uri
    assert arg_dict["EnqueuedURIMetaData"] == didl
    assert arg_dict["InstanceID"] == 0

    # play_from_queue is called with 0-based index; FirstTrackNumberEnqueued
    # from SOAP is 1-based, so "1" → 0.
    coordinator.play_from_queue.assert_called_once_with(0)


def test_play_container_converts_1_based_queue_index() -> None:
    """If the queue wasn't actually empty (e.g. clear_queue is a mock that
    does nothing to the mock's state), FirstTrackNumberEnqueued could be
    greater than 1. Verify the 1→0-based conversion still holds."""
    coordinator = _make_coordinator()
    coordinator.avTransport.AddURIToQueue.return_value = {
        "FirstTrackNumberEnqueued": "11",  # 10 tracks already in queue
        "NumTracksAdded": "50",
        "NewQueueLength": "60",
    }
    _play_container(
        coordinator,
        "x-rincon-cpcontainer:foo",
        "<DIDL-Lite></DIDL-Lite>",
    )
    coordinator.play_from_queue.assert_called_once_with(10)


def test_play_container_rejects_empty_didl() -> None:
    """A cpcontainer URI without DIDL is always a bug upstream —
    Sonos won't expand the container without the metadata envelope.
    Fail loudly rather than silently enqueueing an unplayable item."""
    coordinator = _make_coordinator()
    with pytest.raises(ValueError, match="requires DIDL metadata"):
        _play_container(coordinator, "x-rincon-cpcontainer:foo", "")
    # Shouldn't have touched the queue at all
    coordinator.clear_queue.assert_not_called()
    coordinator.avTransport.AddURIToQueue.assert_not_called()


async def test_play_uri_routes_cpcontainer_through_queue_path() -> None:
    """End-to-end: a ``PlayRequest`` with an ``x-rincon-cpcontainer:`` URI
    from ``resolve_playable`` must reach ``_play_container``, not
    ``coordinator.play_uri``. This is the bug the whole series fixes."""
    backend = SonosSpeaker()
    coordinator = _make_coordinator(uid="RINCON_CCC")

    # Minimal device — its only job is to resolve back to the coordinator
    # via the group property.
    device = MagicMock()
    device.uid = "RINCON_AAA"
    device.player_name = "Kitchen"
    device.group = SimpleNamespace(coordinator=coordinator)
    backend._devices = {"RINCON_AAA": device}

    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        "<item><upnp:class>object.container.playlistContainer</upnp:class></item>"
        "</DIDL-Lite>"
    )
    request = PlayRequest(
        uri="x-rincon-cpcontainer:0fffffffspotify%3Aplaylist%3AabcDEF",
        speaker_ids=["RINCON_AAA"],
        didl_meta=didl,
        title="Blues Rock Mix",
    )
    await backend.play_uri(request)

    # Container path: queue ops called, direct play_uri NOT called.
    coordinator.clear_queue.assert_called_once_with()
    coordinator.avTransport.AddURIToQueue.assert_called_once()
    coordinator.play_from_queue.assert_called_once_with(0)
    coordinator.play_uri.assert_not_called()


async def test_play_uri_track_still_uses_direct_play_path() -> None:
    """Regression guard: the fix must not break the track path. A bare
    ``spotify:track:...`` URI should still be converted to
    ``x-sonos-spotify:`` and played via ``coordinator.play_uri``, not
    enqueued."""
    backend = SonosSpeaker()
    backend._spotify_sn = 5
    coordinator = _make_coordinator(uid="RINCON_CCC")
    device = MagicMock()
    device.uid = "RINCON_AAA"
    device.player_name = "Kitchen"
    device.group = SimpleNamespace(coordinator=coordinator)
    backend._devices = {"RINCON_AAA": device}

    request = PlayRequest(
        uri="spotify:track:3w0pyHgJJW9JN0cJxmi33Z",
        speaker_ids=["RINCON_AAA"],
        title="Always and Forever",
    )
    await backend.play_uri(request)

    # Direct play_uri path — queue ops NOT touched.
    coordinator.clear_queue.assert_not_called()
    coordinator.avTransport.AddURIToQueue.assert_not_called()
    coordinator.play_from_queue.assert_not_called()

    # play_uri called with the converted x-sonos-spotify URI.
    coordinator.play_uri.assert_called_once()
    called_uri = coordinator.play_uri.call_args[0][0]
    assert called_uri.startswith("x-sonos-spotify:")
    assert "spotify%3atrack%3a3w0pyHgJJW9JN0cJxmi33Z" in called_uri
