"""Tests for UniFi console discovery and the doorbell backend's self-healing.

The failure these cover: a UNVR's DHCP lease changed, the configured host
went dead, and every poll logged an identical warning for months without
anyone noticing the console was sitting at a new address the whole time.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from gilbert_plugin_unifi.client import UniFiClient, UniFiConnectionError
from gilbert_plugin_unifi.discovery import (
    DiscoveredController,
    discover_controllers,
    find_controller,
    normalize_mac,
    probe_controller,
)
from gilbert_plugin_unifi.doorbell import UniFiProtectDoorbellBackend

# A console at .10 with Protect, a console at .11 without, and a printer.
_UNVR = {"hardware": {"shortname": "UNVR"}, "name": "Test UNVR", "mac": "74FA29204D61"}
_UDM = {"hardware": {"shortname": "UDMPROSE"}, "name": "Test UDM", "mac": "60223289A81C"}


def _fake_network(hosts: dict[str, dict]) -> httpx.AsyncClient:
    """Build a client whose transport answers only for ``hosts``.

    ``hosts`` maps an address to ``{"system": <json|None>, "protect": <status>}``.
    Anything else raises ConnectError, like an address with nothing on it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        entry = hosts.get(request.url.host)
        if entry is None:
            raise httpx.ConnectError("unreachable", request=request)
        if request.url.path == "/api/system":
            system = entry.get("system")
            if system is None:
                return httpx.Response(404)
            return httpx.Response(200, json=system)
        if request.url.path == "/proxy/protect/api/bootstrap":
            return httpx.Response(entry.get("protect", 200))
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestNormalizeMac:
    @pytest.mark.parametrize(
        "raw",
        ["74FA29204D61", "74:fa:29:20:4d:61", "74-FA-29-20-4D-61"],
    )
    def test_separator_styles_compare_equal(self, raw: str) -> None:
        assert normalize_mac(raw) == "74fa29204d61"


class TestProbeController:
    @pytest.mark.asyncio
    async def test_identifies_a_protect_console(self) -> None:
        async with _fake_network(
            {"192.168.1.10": {"system": _UNVR, "protect": 401}}
        ) as client:
            found = await probe_controller(client, "192.168.1.10")

        assert found is not None
        assert found.host == "https://192.168.1.10"
        assert found.shortname == "UNVR"
        assert found.mac == "74fa29204d61"
        assert found.has_protect is True

    @pytest.mark.asyncio
    async def test_console_without_protect_serves_the_portal_spa(self) -> None:
        """A 200 on the bootstrap probe is the SPA fallback, not Protect."""
        async with _fake_network(
            {"192.168.1.11": {"system": _UDM, "protect": 200}}
        ) as client:
            found = await probe_controller(client, "192.168.1.11")

        assert found is not None
        assert found.has_protect is False

    @pytest.mark.asyncio
    async def test_non_unifi_host_is_ignored(self) -> None:
        async with _fake_network(
            {"192.168.1.50": {"system": {"model": "printer"}}}
        ) as client:
            assert await probe_controller(client, "192.168.1.50") is None

    @pytest.mark.asyncio
    async def test_unreachable_address_is_ignored(self) -> None:
        async with _fake_network({}) as client:
            assert await probe_controller(client, "192.168.1.99") is None


class TestDiscoverControllers:
    @pytest.mark.asyncio
    async def test_protect_consoles_sort_first(self) -> None:
        network = {
            "192.168.1.10": {"system": _UDM, "protect": 200},
            "192.168.1.11": {"system": _UNVR, "protect": 401},
        }
        async with _fake_network(network) as client:
            with patch(
                "gilbert_plugin_unifi.discovery.candidate_addresses",
                return_value=list(network),
            ):
                found = await discover_controllers(client=client)

        assert [c.shortname for c in found] == ["UNVR", "UDMPROSE"]

    @pytest.mark.asyncio
    async def test_find_controller_prefers_the_pinned_mac(self) -> None:
        """Re-homing must follow the MAC, not "whoever answers first"."""
        network = {
            "192.168.1.10": {"system": _UNVR, "protect": 401},
            "192.168.1.11": {"system": _UDM, "protect": 200},
        }
        async with _fake_network(network) as client:
            with patch(
                "gilbert_plugin_unifi.discovery.candidate_addresses",
                return_value=list(network),
            ):
                found = await find_controller(
                    mac="60:22:32:89:a8:1c", client=client
                )

        assert found is not None
        assert found.host == "https://192.168.1.11"

    @pytest.mark.asyncio
    async def test_pinned_mac_absent_returns_none_rather_than_wrong_console(
        self,
    ) -> None:
        network = {"192.168.1.10": {"system": _UNVR, "protect": 401}}
        async with _fake_network(network) as client:
            with patch(
                "gilbert_plugin_unifi.discovery.candidate_addresses",
                return_value=list(network),
            ):
                found = await find_controller(mac="aa:bb:cc:dd:ee:ff", client=client)

        assert found is None


class TestClientRehoming:
    def test_set_host_repoints_and_drops_the_session(self) -> None:
        client = UniFiClient("192.168.1.10", "user", "pass")
        client._logged_in = True

        client.set_host("https://192.168.1.11")

        assert client.host == "https://192.168.1.11"
        assert client._logged_in is False
        assert str(client._client.base_url).startswith("https://192.168.1.11")

    def test_set_host_normalizes_scheme(self) -> None:
        client = UniFiClient("192.168.1.10", "user", "pass")
        client.set_host("http://192.168.1.11")
        assert client.host == "https://192.168.1.11"


class TestDoorbellAutoDiscovery:
    @pytest.mark.asyncio
    async def test_blank_host_discovers_a_console(self) -> None:
        backend = UniFiProtectDoorbellBackend()
        controller = DiscoveredController(
            host="https://192.168.1.11",
            name="Test UNVR",
            shortname="UNVR",
            mac="74fa29204d61",
            has_protect=True,
        )

        with patch(
            "gilbert_plugin_unifi.doorbell.find_controller",
            AsyncMock(return_value=controller),
        ):
            await backend.initialize(
                {"host": "", "username": "u", "password": "p"}
            )

        assert backend._client is not None
        assert backend._client.host == "https://192.168.1.11"

    @pytest.mark.asyncio
    async def test_blank_host_without_auto_discover_stays_uninitialized(self) -> None:
        backend = UniFiProtectDoorbellBackend()
        with patch(
            "gilbert_plugin_unifi.doorbell.find_controller",
            AsyncMock(side_effect=AssertionError("must not discover")),
        ):
            await backend.initialize(
                {
                    "host": "",
                    "username": "u",
                    "password": "p",
                    "auto_discover": False,
                }
            )
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_connection_failure_rehomes_to_the_new_address(self) -> None:
        """The production failure: the console moved and polling never recovered."""
        backend = UniFiProtectDoorbellBackend()
        await backend.initialize(
            {"host": "192.168.1.10", "username": "u", "password": "p"}
        )
        assert backend._client is not None
        assert backend._protect is not None

        backend._protect.get_detection_events = AsyncMock(  # type: ignore[method-assign]
            side_effect=UniFiConnectionError("Cannot reach https://192.168.1.10")
        )
        backend._access = None

        moved = DiscoveredController(
            host="https://192.168.1.11",
            name="Test UNVR",
            shortname="UNVR",
            mac="74fa29204d61",
            has_protect=True,
        )
        with patch(
            "gilbert_plugin_unifi.doorbell.find_controller",
            AsyncMock(return_value=moved),
        ):
            assert await backend.get_ring_events() == []

        assert backend._client.host == "https://192.168.1.11"

    @pytest.mark.asyncio
    async def test_rediscovery_is_rate_limited(self) -> None:
        """A sweep per failed poll would be worse than the outage itself."""
        backend = UniFiProtectDoorbellBackend()
        await backend.initialize(
            {"host": "192.168.1.10", "username": "u", "password": "p"}
        )
        assert backend._protect is not None
        backend._protect.get_detection_events = AsyncMock(  # type: ignore[method-assign]
            side_effect=UniFiConnectionError("Cannot reach https://192.168.1.10")
        )
        backend._access = None

        finder = AsyncMock(return_value=None)
        with patch("gilbert_plugin_unifi.doorbell.find_controller", finder):
            for _ in range(5):
                await backend.get_ring_events()

        assert finder.await_count == 1, (
            f"expected one discovery sweep across five polls, got {finder.await_count}"
        )


class TestRepeatedWarningThrottle:
    def test_identical_failure_logs_once(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = UniFiProtectDoorbellBackend()
        with caplog.at_level("WARNING", logger="gilbert_plugin_unifi.doorbell"):
            for _ in range(100):
                backend._warn_once("k", "poll failed: %s", "unreachable")

        assert len(caplog.records) == 1, (
            f"a repeating failure must not spam the log, got {len(caplog.records)}"
        )

    def test_a_changed_message_still_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = UniFiProtectDoorbellBackend()
        with caplog.at_level("WARNING", logger="gilbert_plugin_unifi.doorbell"):
            backend._warn_once("k", "poll failed: %s", "unreachable")
            backend._warn_once("k", "poll failed: %s", "auth rejected")

        assert len(caplog.records) == 2

    def test_throttle_state_is_bounded(self) -> None:
        backend = UniFiProtectDoorbellBackend()
        for i in range(200):
            backend._warn_once("k", "poll failed: %s", f"error-{i}")
        assert len(backend._warned) <= 32
