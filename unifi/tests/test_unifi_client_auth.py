"""Tests for UniFiClient authentication modes — API key vs cookie login."""

from __future__ import annotations

import httpx
import pytest
from gilbert_plugin_unifi.client import UniFiAuthError, UniFiClient
from gilbert_plugin_unifi.presence import UniFiPresenceBackend


def _json(payload: dict[str, object], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _mock(client: UniFiClient, handler) -> list[httpx.Request]:
    """Swap a MockTransport into an already-built client, recording requests."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client._client._transport = httpx.MockTransport(_record)
    return seen


class TestHostNormalization:
    def test_bare_host_upgraded_to_https(self) -> None:
        assert UniFiClient("192.168.1.1", api_key="k").host == "https://192.168.1.1"

    def test_http_upgraded_to_https(self) -> None:
        assert UniFiClient("http://192.168.1.1", api_key="k").host == "https://192.168.1.1"

    def test_trailing_slash_stripped(self) -> None:
        assert UniFiClient("https://192.168.1.1/", api_key="k").host == "https://192.168.1.1"


class TestApiKeyMode:
    def test_uses_api_key_flag(self) -> None:
        assert UniFiClient("h", api_key="secret").uses_api_key is True
        assert UniFiClient("h", username="u", password="p").uses_api_key is False

    def test_api_key_header_is_set(self) -> None:
        c = UniFiClient("h", api_key="secret")
        assert c._client.headers.get("x-api-key") == "secret"

    def test_no_api_key_header_without_key(self) -> None:
        c = UniFiClient("h", username="u", password="p")
        assert "x-api-key" not in c._client.headers

    @pytest.mark.asyncio
    async def test_login_is_a_noop(self) -> None:
        c = UniFiClient("h", api_key="secret")
        seen = _mock(c, lambda r: _json({}))
        await c.login()
        assert seen == []  # no /api/auth/login round-trip
        await c.close()

    @pytest.mark.asyncio
    async def test_get_sends_key_and_skips_login(self) -> None:
        c = UniFiClient("h", api_key="secret")
        seen = _mock(c, lambda r: _json({"data": [{"mac": "aa"}]}))
        out = await c.get("/proxy/network/api/s/default/stat/sta")
        assert out == {"data": [{"mac": "aa"}]}
        assert len(seen) == 1
        assert seen[0].url.path == "/proxy/network/api/s/default/stat/sta"
        assert seen[0].headers.get("x-api-key") == "secret"
        await c.close()

    @pytest.mark.asyncio
    async def test_401_raises_without_retry(self) -> None:
        """A key has no session to refresh — 401 is terminal, not a re-login cue."""
        c = UniFiClient("h", api_key="wrong-scope")
        seen = _mock(c, lambda r: _json({"error": "nope"}, status=401))
        with pytest.raises(UniFiAuthError, match="API key rejected"):
            await c.get("/proxy/network/api/s/default/stat/sta")
        assert len(seen) == 1  # no retry, no login attempt
        await c.close()

    @pytest.mark.asyncio
    async def test_api_key_wins_over_username_password(self) -> None:
        c = UniFiClient("h", username="u", password="p", api_key="secret")
        seen = _mock(c, lambda r: _json({"ok": True}))
        await c.get("/x")
        assert len(seen) == 1
        assert seen[0].url.path == "/x"  # never hit /api/auth/login
        await c.close()


class TestCookieMode:
    @pytest.mark.asyncio
    async def test_login_then_request(self) -> None:
        c = UniFiClient("h", username="u", password="p")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                return _json({"ok": True})
            return _json({"data": []})

        seen = _mock(c, handler)
        out = await c.get("/proxy/network/api/s/default/stat/sta")
        assert out == {"data": []}
        assert [r.url.path for r in seen] == [
            "/api/auth/login",
            "/proxy/network/api/s/default/stat/sta",
        ]
        await c.close()

    @pytest.mark.asyncio
    async def test_401_triggers_relogin_and_retry(self) -> None:
        c = UniFiClient("h", username="u", password="p")
        c._logged_in = True  # pretend we have a stale cookie
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/api/auth/login":
                return _json({"ok": True})
            # 401 the first data call, succeed after re-login
            if calls.count("/data") == 1:
                return _json({"err": 1}, status=401)
            return _json({"data": "fresh"})

        _mock(c, handler)
        assert await c.get("/data") == {"data": "fresh"}
        assert calls == ["/data", "/api/auth/login", "/data"]
        await c.close()


class TestBackendCredentialResolution:
    """``_get_or_create_client`` must accept an api_key with no user/password."""

    @pytest.mark.asyncio
    async def test_api_key_only_builds_a_client(self) -> None:
        backend = UniFiPresenceBackend()
        client = await backend._get_or_create_client(
            {"host": "192.168.86.1", "api_key": "secret"}
        )
        assert client is not None
        assert client.uses_api_key is True
        await backend.close()

    @pytest.mark.asyncio
    async def test_no_credentials_returns_none(self) -> None:
        backend = UniFiPresenceBackend()
        assert await backend._get_or_create_client({"host": "192.168.86.1"}) is None

    @pytest.mark.asyncio
    async def test_username_without_password_returns_none(self) -> None:
        backend = UniFiPresenceBackend()
        assert (
            await backend._get_or_create_client({"host": "h", "username": "u", "password": ""})
            is None
        )

    @pytest.mark.asyncio
    async def test_client_is_reused_per_host(self) -> None:
        backend = UniFiPresenceBackend()
        cfg = {"host": "192.168.86.1", "api_key": "secret"}
        first = await backend._get_or_create_client(cfg)
        second = await backend._get_or_create_client(cfg)
        assert first is second
        await backend.close()


class TestConfigParams:
    def test_network_api_key_param_is_exposed_and_sensitive(self) -> None:
        params = {p.key: p for p in UniFiPresenceBackend.backend_config_params()}
        assert "unifi_network.api_key" in params
        p = params["unifi_network.api_key"]
        assert p.sensitive is True
        assert p.restart_required is True
        assert p.default == ""
