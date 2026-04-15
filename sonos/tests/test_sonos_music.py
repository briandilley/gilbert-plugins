"""Tests for the Sonos music backend — SMAPI id → Spotify URI resolution."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from gilbert_plugin_sonos.sonos_music import (
    SonosMusic,
    _build_spotify_container_playable,
    _extract_spotify_uri,
    _smapi_result_to_music_item,
    _spotify_kind,
)

from gilbert.interfaces.music import MusicItem, MusicItemKind

# ── _extract_spotify_uri ────────────────────────────────────────────


class TestExtractSpotifyUri:
    def test_bare_spotify_track(self) -> None:
        """A canonical ``spotify:track:<id>`` passes straight through."""
        assert (
            _extract_spotify_uri("spotify:track:3w0pyHgJJW9JN0cJxmi33Z")
            == "spotify:track:3w0pyHgJJW9JN0cJxmi33Z"
        )

    def test_flags_prefix_stripped(self) -> None:
        """SMAPI prepends an 8-char hex flags prefix that we strip."""
        assert (
            _extract_spotify_uri(
                "0fffffffspotify:track:3w0pyHgJJW9JN0cJxmi33Z",
            )
            == "spotify:track:3w0pyHgJJW9JN0cJxmi33Z"
        )

    def test_percent_encoded_colons(self) -> None:
        """``%3a`` is how the colon appears inside a ``soco://`` URI."""
        assert (
            _extract_spotify_uri(
                "0fffffffspotify%3Atrack%3A3w0pyHgJJW9JN0cJxmi33Z",
            )
            == "spotify:track:3w0pyHgJJW9JN0cJxmi33Z"
        )

    def test_double_encoded(self) -> None:
        """The actual failure on disk double-encoded to ``%253A``.

        That's `%` (the `%25`) then the literal `3A`. Decoding one layer
        leaves ``%3A`` which the regex handles.
        """
        # Note: our extractor operates on whatever the caller hands it —
        # if the caller only does one round of URL decoding, we still get
        # the right answer via the %3[Aa] branch. The outer %25 won't
        # match here but the inner %3A (what the caller passed) will.
        assert (
            _extract_spotify_uri(
                "spotify%3Atrack%3A3w0pyHgJJW9JN0cJxmi33Z",
            )
            == "spotify:track:3w0pyHgJJW9JN0cJxmi33Z"
        )

    def test_album(self) -> None:
        assert (
            _extract_spotify_uri("0abcdef0spotify:album:1DFixLWuPkv3KT3TnV35m3")
            == "spotify:album:1DFixLWuPkv3KT3TnV35m3"
        )

    def test_playlist(self) -> None:
        assert (
            _extract_spotify_uri(
                "0fffffffspotify%3Aplaylist%3A37i9dQZF1DXcBWIGoYBM5M",
            )
            == "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
        )

    def test_artist(self) -> None:
        assert (
            _extract_spotify_uri("spotify:artist:0du5cEVh5yTK9QJze8zA0C")
            == "spotify:artist:0du5cEVh5yTK9QJze8zA0C"
        )

    def test_returns_none_for_non_spotify(self) -> None:
        assert _extract_spotify_uri("apple_music:song:123456") is None
        assert _extract_spotify_uri("amazon:ASIN:B0000000") is None
        assert _extract_spotify_uri("") is None
        assert _extract_spotify_uri("some-opaque-id-no-scheme") is None

    def test_rejects_invalid_spotify_kind(self) -> None:
        """Only known kinds match — a typo shouldn't silently slip through."""
        assert _extract_spotify_uri("spotify:trak:abc") is None


# ── resolve_playable wiring ─────────────────────────────────────────


async def test_resolve_playable_uses_spotify_fast_path() -> None:
    """SMAPI search result → clean spotify URI, no call to sonos_uri_from_id.

    This is the regression test for the ``UPnP Error 714 Illegal
    MIME-Type`` failure we hit on ``play_uri``: the old code handed
    the speaker backend a ``soco://`` URI that Sonos rejected. The
    new code pulls the embedded ``spotify:<kind>:<id>`` out of the
    SMAPI id so the speaker backend's ``_to_sonos_spotify_uri`` can
    build a real ``x-sonos-spotify:...`` playback URI.
    """
    backend = SonosMusic()
    # Poison the SMAPI path — if it's called at all the test fails.
    backend._get_smapi = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError(
            "sonos_uri_from_id should not be called for Spotify items",
        ),
    )

    item = MusicItem(
        id="0fffffffspotify:track:3w0pyHgJJW9JN0cJxmi33Z",
        title="Always and Forever",
        kind=MusicItemKind.TRACK,
        subtitle="Heatwave",
        uri="",
        service="Spotify",
    )
    playable = await backend.resolve_playable(item)

    assert playable.uri == "spotify:track:3w0pyHgJJW9JN0cJxmi33Z"
    assert playable.title == "Always and Forever"


async def test_resolve_playable_passes_through_direct_uri() -> None:
    """Favorites already have a URI — don't touch them."""
    backend = SonosMusic()
    item = MusicItem(
        id="fav-123",
        title="Morning Playlist",
        kind=MusicItemKind.PLAYLIST,
        subtitle="",
        uri="x-rincon-cpcontainer:1006206cplaylist:abc",
        didl_meta="<DIDL>...</DIDL>",
        service="Sonos",
    )
    playable = await backend.resolve_playable(item)
    assert playable.uri == "x-rincon-cpcontainer:1006206cplaylist:abc"
    assert playable.didl_meta == "<DIDL>...</DIDL>"
    assert playable.title == "Morning Playlist"


async def test_resolve_playable_container_only_favorite() -> None:
    """Container favorites without a URI carry only DIDL — preserve it."""
    backend = SonosMusic()
    item = MusicItem(
        id="",
        title="Bedroom Radio",
        kind=MusicItemKind.STATION,
        subtitle="",
        uri="",
        didl_meta="<DIDL-Lite><item>...</item></DIDL-Lite>",
        service="TuneIn",
    )
    playable = await backend.resolve_playable(item)
    assert playable.uri == ""
    assert playable.didl_meta == "<DIDL-Lite><item>...</item></DIDL-Lite>"


async def test_resolve_playable_non_spotify_falls_back_to_smapi() -> None:
    """Non-Spotify SMAPI services still use the legacy path."""
    backend = SonosMusic()

    fake_svc = MagicMock()
    fake_svc.sonos_uri_from_id.return_value = "soco://apple_music:song:123?sid=52&sn=1"
    backend._get_smapi = MagicMock(return_value=fake_svc)  # type: ignore[method-assign]

    item = MusicItem(
        id="apple_music:song:123",
        title="Some Apple Song",
        kind=MusicItemKind.TRACK,
        subtitle="",
        uri="",
        service="Apple Music",
    )
    playable = await backend.resolve_playable(item)
    assert playable.uri == "soco://apple_music:song:123?sid=52&sn=1"
    fake_svc.sonos_uri_from_id.assert_called_once_with("apple_music:song:123")


async def test_resolve_playable_raises_when_no_uri_and_no_id() -> None:
    backend = SonosMusic()
    item = MusicItem(
        id="",
        title="Broken",
        kind=MusicItemKind.TRACK,
        subtitle="",
        uri="",
        service="Sonos",
    )
    with pytest.raises(ValueError, match="no uri and no id"):
        await backend.resolve_playable(item)


# ── Spotify container playback ──────────────────────────────────────

# The bug this regression suite covers:
#
# Before this fix, ``resolve_playable`` sent *every* Spotify item kind
# (track, playlist, album, artist) down the same fast path — returning
# a bare ``spotify:<kind>:<id>`` URI with no DIDL metadata. The speaker
# backend then built an ``x-sonos-spotify:`` URI with ``flags=8232`` and
# called ``coordinator.play_uri`` directly. That works for tracks, but
# silently no-ops for containers: Sonos accepts the SOAP calls, logs
# nothing, and plays nothing, because a container URI without queue
# context doesn't tell it which track to start.
#
# The /radio DJ search for PLAYLIST kind, so *every* /radio start hit
# this broken path. These tests lock in the new container branch:
# ``resolve_playable`` now builds an ``x-rincon-cpcontainer:`` URI plus
# a DIDL envelope with the right ``upnp:class``, and the speaker backend
# routes those through its AddURIToQueue/play_from_queue path.


class TestSpotifyKind:
    def test_returns_kind_for_known_spotify_ids(self) -> None:
        assert _spotify_kind("spotify:track:abc") == "track"
        assert _spotify_kind("spotify:playlist:abc") == "playlist"
        assert _spotify_kind("spotify:album:abc") == "album"
        assert _spotify_kind("spotify:artist:abc") == "artist"
        assert _spotify_kind("0fffffffspotify%3Aplaylist%3Aabc") == "playlist"

    def test_returns_none_for_non_spotify(self) -> None:
        assert _spotify_kind("") is None
        assert _spotify_kind("apple_music:song:123") is None
        assert _spotify_kind("spotify:trak:abc") is None


class TestBuildSpotifyContainerPlayable:
    """The DIDL shape this produces is the one I manually verified lands on
    real Sonos hardware via ``avTransport.AddURIToQueue`` — NumTracksAdded=50
    for a typical Spotify playlist. Keep the shape in sync with
    experimental hardware verification if you change it."""

    def test_playlist_builds_cpcontainer_uri(self) -> None:
        item = MusicItem(
            id="0fffffffspotify%3Aplaylist%3A37i9dQZF1EIeh2qaJ3IfzG",
            title="Blues Rock Mix",
            kind=MusicItemKind.PLAYLIST,
            subtitle="Spotify",
            uri="",
            service="Spotify",
        )
        playable = _build_spotify_container_playable(item, "playlist")

        # URI uses the raw SMAPI item id as the cpcontainer tail. The
        # speaker backend dispatches on this prefix.
        assert playable.uri == (
            "x-rincon-cpcontainer:0fffffffspotify%3Aplaylist%3A37i9dQZF1EIeh2qaJ3IfzG"
        )
        assert playable.title == "Blues Rock Mix"

        # DIDL must carry the playlistContainer class, or Sonos treats
        # the container URI as a single audioItem and plays nothing.
        didl = playable.didl_meta
        assert "<upnp:class>object.container.playlistContainer</upnp:class>" in didl
        # Real title in <dc:title> (not "DUMMY" like SoCo's default).
        assert "<dc:title>Blues Rock Mix</dc:title>" in didl
        # Service descriptor binds the URI to the linked Spotify account.
        assert "SA_RINCON3079_X_#Svc3079-0-Token" in didl
        # Item id matches the URI tail so Sonos can resolve the container.
        assert 'id="0fffffffspotify%3Aplaylist%3A37i9dQZF1EIeh2qaJ3IfzG"' in didl

    def test_album_uses_album_container_class(self) -> None:
        item = MusicItem(
            id="0fffffffspotify%3Aalbum%3A1DFixLWuPkv3KT3TnV35m3",
            title="Some Album",
            kind=MusicItemKind.ALBUM,
            subtitle="Some Artist",
            uri="",
            service="Spotify",
        )
        playable = _build_spotify_container_playable(item, "album")
        assert playable.uri.startswith("x-rincon-cpcontainer:")
        assert "<upnp:class>object.container.album.musicAlbum</upnp:class>" in playable.didl_meta

    def test_artist_uses_person_container_class(self) -> None:
        item = MusicItem(
            id="0fffffffspotify%3Aartist%3A0du5cEVh5yTK9QJze8zA0C",
            title="Some Artist",
            kind=MusicItemKind.ARTIST,
            subtitle="",
            uri="",
            service="Spotify",
        )
        playable = _build_spotify_container_playable(item, "artist")
        assert playable.uri.startswith("x-rincon-cpcontainer:")
        assert "<upnp:class>object.container.person.musicArtist</upnp:class>" in playable.didl_meta

    def test_escapes_special_chars_in_title(self) -> None:
        """DIDL is XML — titles with < or & must be escaped or the blob breaks."""
        item = MusicItem(
            id="0fffffffspotify%3Aplaylist%3AabcDEF",
            title="R&B <2020>",
            kind=MusicItemKind.PLAYLIST,
            subtitle="",
            uri="",
            service="Spotify",
        )
        playable = _build_spotify_container_playable(item, "playlist")
        assert "<dc:title>R&amp;B &lt;2020&gt;</dc:title>" in playable.didl_meta
        # And the raw string is NOT present unescaped
        assert "<dc:title>R&B <2020></dc:title>" not in playable.didl_meta


# ── SMAPI metadata extraction ────────────────────────────────────────
#
# SMAPI result objects expose their fields via ``item.metadata`` (a
# dict), NOT as attributes on the object. Before this fix,
# ``_smapi_result_to_music_item`` was doing ``getattr(item, "artist")``
# which came back empty for every result, so search rows showed just
# titles with no artist subtitle and no artwork in the chat UI.
#
# Worse, the shape varies by kind: playlists and albums have
# ``artist`` and ``album_art_uri`` inline, but tracks wrap them in a
# ``track_metadata`` object. Both paths need to work.


class _SMAPIStub:
    """Mimics the shape SoCo SMAPI results actually return: ``item_id``
    on the instance and everything else inside a ``.metadata`` dict."""

    def __init__(
        self,
        item_id: str,
        title: str,
        metadata: dict[str, Any],
    ) -> None:
        self.item_id = item_id
        self.title = title
        self.metadata = metadata


class _TrackMetaStub:
    """The TrackMetadata wrapper that SoCo nests inside track results.

    The real class exposes artist/album/album_art_uri both as attributes
    *and* inside a ``.metadata`` dict; mirror the attribute form because
    that's the one our helpers prefer."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)
        self.metadata = dict(fields)


class TestSmapiResultMapping:
    def test_playlist_pulls_artwork_and_artist_from_top_level(self) -> None:
        """Playlists inline ``album_art_uri`` and ``artist`` at the top."""
        raw = _SMAPIStub(
            item_id="0fffffffspotify%3Aplaylist%3AabcDEF",
            title="Blues Rock Mix",
            metadata={
                "id": "spotify:playlist:abcDEF",
                "item_type": "playlist",
                "title": "Blues Rock Mix",
                "artist": "Spotify",
                "album_art_uri": "https://art/blues.jpg",
                "can_play": True,
            },
        )
        item = _smapi_result_to_music_item(raw, MusicItemKind.PLAYLIST, "Spotify")

        assert item.title == "Blues Rock Mix"
        assert item.subtitle == "Spotify"
        assert item.album_art_url == "https://art/blues.jpg"
        assert item.duration_seconds == 0.0  # Containers have no duration

    def test_album_pulls_artist_and_artwork(self) -> None:
        raw = _SMAPIStub(
            item_id="0fffffffspotify%3Aalbum%3AABC",
            title="Four",
            metadata={
                "id": "spotify:album:ABC",
                "item_type": "album",
                "title": "Four",
                "artist": "Blues Traveler",
                "album_art_uri": "https://art/four.jpg",
            },
        )
        item = _smapi_result_to_music_item(raw, MusicItemKind.ALBUM, "Spotify")
        assert item.subtitle == "Blues Traveler"
        assert item.album_art_url == "https://art/four.jpg"

    def test_track_pulls_artist_and_artwork_from_nested_track_metadata(self) -> None:
        """Tracks wrap artist/album/album_art_uri inside track_metadata —
        the top-level metadata doesn't carry them directly."""
        track_meta = _TrackMetaStub(
            artist="Blues Traveler",
            album="Four",
            album_art_uri="https://art/four-cover.jpg",
            duration=279,
        )
        raw = _SMAPIStub(
            item_id="0fffffffspotify%3Atrack%3AXYZ",
            title="Run-Around",
            metadata={
                "id": "spotify:track:XYZ",
                "item_type": "track",
                "title": "Run-Around",
                "track_metadata": track_meta,
            },
        )
        item = _smapi_result_to_music_item(raw, MusicItemKind.TRACK, "Spotify")

        assert item.title == "Run-Around"
        assert item.subtitle == "Blues Traveler"
        assert item.album_art_url == "https://art/four-cover.jpg"
        assert item.duration_seconds == 279.0

    def test_track_without_track_metadata_is_not_fatal(self) -> None:
        """Some minimal SMAPI results come back without ``track_metadata``.
        Missing fields are a degraded UX, not a crash."""
        raw = _SMAPIStub(
            item_id="0fffffffspotify%3Atrack%3AXYZ",
            title="Unknown Song",
            metadata={
                "id": "spotify:track:XYZ",
                "item_type": "track",
                "title": "Unknown Song",
            },
        )
        item = _smapi_result_to_music_item(raw, MusicItemKind.TRACK, "Spotify")
        assert item.title == "Unknown Song"
        assert item.subtitle == ""
        assert item.album_art_url == ""
        assert item.duration_seconds == 0.0

    def test_missing_metadata_dict_returns_defaults(self) -> None:
        """Defensive: ``metadata`` missing entirely (e.g. test stub or
        future SMAPI shape change) still yields a valid MusicItem."""
        raw = _SMAPIStub(item_id="id", title="x", metadata={})
        # Simulate the metadata attribute being missing entirely
        del raw.metadata  # type: ignore[attr-defined]
        item = _smapi_result_to_music_item(raw, MusicItemKind.TRACK, "Spotify")
        assert item.title == "x"
        assert item.album_art_url == ""


async def test_resolve_playable_playlist_uses_container_fast_path() -> None:
    """Regression: the /radio DJ silent-no-op bug.

    Before this fix, ``resolve_playable`` would hand back a bare
    ``spotify:playlist:<id>`` URI for PLAYLIST items, which the speaker
    backend wrapped in ``x-sonos-spotify:...?flags=8232``. Sonos
    accepted the SOAP call but played nothing, because that URI shape
    only works for single tracks. The radio DJ log showed "Playing ...
    on Lobby" and the user heard silence.
    """
    backend = SonosMusic()
    # Poison the SMAPI path — containers shouldn't touch sonos_uri_from_id.
    backend._get_smapi = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError(
            "sonos_uri_from_id should not be called for Spotify containers",
        ),
    )

    item = MusicItem(
        id="0fffffffspotify%3Aplaylist%3A37i9dQZF1EIeh2qaJ3IfzG",
        title="Blues Rock Mix",
        kind=MusicItemKind.PLAYLIST,
        subtitle="Spotify",
        uri="",
        service="Spotify",
    )
    playable = await backend.resolve_playable(item)

    assert playable.uri.startswith("x-rincon-cpcontainer:")
    assert "<upnp:class>object.container.playlistContainer</upnp:class>" in playable.didl_meta
    assert playable.title == "Blues Rock Mix"
