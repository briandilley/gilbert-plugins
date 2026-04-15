# Sonos Spotify Playback — Tracks vs Containers

## Summary
Single items (tracks/episodes/shows) and containers (playlists/albums/artists)
take *different* playback paths on Sonos. Sending a container URI to
`SetAVTransportURI`/`play_uri` silently no-ops — the SOAP call returns 200,
nothing throws, and no audio plays. Containers must be routed through
`AddURIToQueue` + `play_from_queue`.

## Details

**The silent-no-op footgun.** `coordinator.play_uri(uri, title=…)` with no
`meta=` argument makes SoCo fabricate a default DIDL envelope whose
`upnp:class` is `object.item.audioItem.musicTrack`. Sonos therefore treats
whatever URI it gets as a single track. For `spotify:track:…` that works.
For `spotify:playlist:…` / `spotify:album:…` / `spotify:artist:…` Sonos
accepts the AVTransport action, logs nothing, and plays nothing — it has
a container URI but metadata saying it's a single track, so there's no
queue to start from. The Radio DJ's "Playing … on Lobby" info line fires
because the `play_uri` call completed without raising, even though no
audio ever reached the speakers.

**The two paths.**

*Tracks* — direct `play_uri`:

```
x-sonos-spotify:spotify%3atrack%3a<ID>?sid=12&flags=8232&sn=<n>
```

Built by `sonos_speaker._to_sonos_spotify_uri`. Fed straight to
`coordinator.play_uri(uri, title=…)` via SoCo; SoCo's default DIDL
envelope (musicTrack class) is correct for this case.

*Containers* — enqueue and play from queue:

```
x-rincon-cpcontainer:<smapi_item_id>
```

Use the raw SMAPI item id (with its 8-char flags prefix like `0fffffff`
and uppercase `%3A` encoding) *verbatim* as the URI tail. `SoCo.to_element()`
for a Spotify playlist SMAPI result returns this same form, so it's the
shape the ecosystem agrees on.

The URI alone isn't enough — it must be paired with a DIDL-Lite envelope:

```xml
<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"
           xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"
           xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"
           xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">
  <item id="<smapi_item_id>" parentID="-1" restricted="true">
    <dc:title><escaped title></dc:title>
    <upnp:class>object.container.playlistContainer</upnp:class>
    <desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON3079_X_#Svc3079-0-Token</desc>
  </item>
</DIDL-Lite>
```

The `upnp:class` varies by kind:
- playlist → `object.container.playlistContainer`
- album → `object.container.album.musicAlbum`
- artist → `object.container.person.musicArtist`

The `<desc>` service descriptor uses Spotify's SMAPI service type (3079).
The literal `SA_RINCON3079_X_#Svc3079-0-Token` is correct — `_X_` is a
placeholder Sonos substitutes at runtime, and no account serial goes in
this string (the `sn=` that shows up in track URIs isn't needed here).

**Playback sequence.** Clear the queue, enqueue, play from the first
newly-enqueued index (1-based → 0-based conversion):

```python
coordinator.clear_queue()
resp = coordinator.avTransport.AddURIToQueue([
    ("InstanceID", 0),
    ("EnqueuedURI", container_uri),
    ("EnqueuedURIMetaData", didl),
    ("DesiredFirstTrackNumberEnqueued", 0),
    ("EnqueueAsNext", 0),
])
first = int(resp["FirstTrackNumberEnqueued"])  # 1-based
coordinator.play_from_queue(first - 1)
```

Experimentally verified against real Lobby hardware: `AddURIToQueue`
for a typical Spotify playlist returns `NumTracksAdded=50` —
Sonos expands the container into individual tracks on the queue —
and `play_from_queue(0)` starts the first one.

**Where the split lives in code.**
- `sonos_music.SonosMusic.resolve_playable` — dispatches on
  `_spotify_kind(item.id)`. Container kinds go through
  `_build_spotify_container_playable` (returns a ready-made
  `x-rincon-cpcontainer:` URI + DIDL). Track/episode/show kinds
  return the clean `spotify:<kind>:<id>` URI for the speaker backend
  to wrap with `_to_sonos_spotify_uri`.
- `sonos_speaker.SonosSpeaker.play_uri` — dispatches on URI scheme.
  `x-rincon-cpcontainer:` routes to `_play_container` (queue path);
  everything else uses the existing direct `play_uri` path.

## Related
- `std-plugins/sonos/sonos_music.py` — `_build_spotify_container_playable`, `_spotify_kind`, `resolve_playable`
- `std-plugins/sonos/sonos_speaker.py` — `_play_container`, `_to_sonos_spotify_uri`, `play_uri`
- `std-plugins/sonos/tests/test_sonos_music.py` — `TestBuildSpotifyContainerPlayable`, `test_resolve_playable_playlist_uses_container_fast_path`
- `std-plugins/sonos/tests/test_sonos_speaker.py` — `test_play_container_*`, `test_play_uri_routes_cpcontainer_through_queue_path`
- Prior commit `c5efe65` — "Fix Sonos-Spotify play_uri 714 Illegal MIME-Type" — introduced the Spotify fast path for tracks but didn't distinguish containers, which is how this bug slipped in.
