# Sonos uses aiosonos (S2-only); SoCo/UPnP and S1 support dropped

The Sonos integration is built on `aiosonos` and supports only S2 systems. This eliminates the UPnP
footguns of the old SoCo path — DIDL handling, `ERROR 714`/`800` failures, the manual
snapshot/restore ritual — in favor of declarative grouping and self-restoring audio clips.

## Consequences

- Older **S1-only hardware no longer works**.
- Legacy `snapshot`/`restore` calls are now no-ops kept only for interface compatibility.
- Spotify playback is the one place the clean library path doesn't work and still needs raw SOAP —
  see [ADR-0005](./0005-spotify-on-sonos-smapi-soap-bridge.md).
