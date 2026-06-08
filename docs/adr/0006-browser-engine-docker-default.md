# Browser engine runs in a Playwright Docker container by default, host-native as fallback

The browser plugin defaults to Microsoft's official Playwright container — which bakes in all the OS
shared libraries Chromium needs, keeping the host clean — and falls back to host-native Playwright
when Docker is unavailable (`mode=auto`). The image tag is auto-pinned to the installed `playwright`
version. One shared container hosts **one `BrowserContext` per user**, serialized by a per-user
`asyncio.Lock`; browser storage state and screenshots round-trip over the WS protocol rather than
volume mounts.

## Consequences

- A Docker dependency (with a host-native fallback) and WS round-tripping of `storage_state`/
  screenshots, traded against not having to `apt-get` Chromium's OS libs onto the host.
- One shared process is a blast-radius trade for the memory savings (~50–100 MB/user, capped at 8).
- Per-user credentials are Fernet-encrypted with a per-install key resolved server-side, so
  passwords never enter an AI prompt — but losing the key makes them unrecoverable. (VNC live-login
  stays host-native regardless of mode.)
