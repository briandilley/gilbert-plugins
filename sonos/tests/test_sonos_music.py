"""Tests for the Sonos music backend — SMAPI id → Spotify URI resolution."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gilbert.interfaces.music import MusicItem, MusicItemKind
from gilbert_plugin_sonos.sonos_music import (
    SonosMusic,
    _extract_spotify_uri,
)

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
    fake_svc.sonos_uri_from_id.return_value = (
        "soco://apple_music:song:123?sid=52&sn=1"
    )
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
