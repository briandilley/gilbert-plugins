# Spotify-on-Sonos uses a hand-rolled SMAPI-over-UPnP-SOAP bridge

Queuing arbitrary Spotify tracks on current Sonos S2 firmware goes through legacy port-1400 UPnP
SOAP, with DIDL descriptors carrying the correct SMAPI service number (`sn` 2311 world / 3079 US) to
route the URI to the right music service. The clean paths don't work: `aiosonos`'s `loadContent`
returns `ERROR_COMMAND_FAILED` on current firmware, and Sonos staff confirmed the Cloud Control API
cannot queue music-service tracks.

## Consequences

This is brittle, firmware-coupled, and undocumented — magic service numbers, raw DIDL, a try-then-
fallback. It is deliberately isolated in `sonos_smapi.py` so the rest of the Sonos integration stays
on the clean `aiosonos` API (see [ADR-0004](./0004-sonos-aiosonos-s2-only.md)).
