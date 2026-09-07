"""Discovery of UniFi OS consoles on the local network.

UniFi consoles (UDM, UDR, UNVR, Cloud Key Gen2+) normally get their address
from DHCP, so a hardcoded controller URL rots the moment the lease changes —
and the only symptom is a connection error on every poll. This module finds
consoles by their identity instead of their address.

Two facts make discovery cheap and unauthenticated:

- ``GET /api/system`` on a UniFi console returns hardware shortname, the
  console's display name, and its MAC **without** a session. That MAC is the
  stable identity we re-home against when an address changes.
- ``GET /proxy/protect/api/bootstrap`` answers ``401`` when Protect is
  installed. A console without Protect serves the UniFi OS portal SPA
  (``200`` + HTML) for the same path, so the 401 is the positive signal —
  a success status here means the route does *not* exist.

Discovery is a sweep of the local ``/24``. Ubiquiti's UDP broadcast protocol
(port 10001) would be faster, but the sweep needs no privileged socket, works
the same when the console is on a different VLAN reachable by unicast, and
identifies Protect in the same pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
import ssl
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

SYSTEM_PATH = "/api/system"
PROTECT_PROBE_PATH = "/proxy/protect/api/bootstrap"

# Sweeping 254 addresses against unreachable hosts is dominated by the
# connect timeout, so keep it short and the fan-out wide.
DEFAULT_PROBE_TIMEOUT = 2.0
DEFAULT_CONCURRENCY = 64

# httpx wraps most transport failures, but some TLS handshake errors
# against consumer gear surface as a bare ssl.SSLError.
_PROBE_ERRORS = (httpx.HTTPError, ssl.SSLError, OSError)


@dataclass(frozen=True)
class DiscoveredController:
    """A UniFi OS console found on the network."""

    host: str
    """Base URL, e.g. ``https://192.168.1.20``."""

    name: str
    """Console display name, e.g. ``Community 1.0 UNVR``."""

    shortname: str
    """Hardware model shortname, e.g. ``UNVR`` / ``UDMPROSE``."""

    mac: str
    """Normalized MAC (lowercase hex, no separators) — the stable identity."""

    has_protect: bool
    """Whether UniFi Protect is installed on this console."""

    def describe(self) -> str:
        label = self.name or self.shortname or "UniFi console"
        suffix = " with Protect" if self.has_protect else ""
        return f"{label} ({self.shortname}){suffix} at {self.host}"


def normalize_mac(raw: str) -> str:
    """Reduce a MAC to lowercase hex so separator styles compare equal.

    UniFi reports MACs both as ``74FA29204D61`` (``/api/system``) and
    ``74:fa:29:20:4d:61`` (most other endpoints).
    """
    return "".join(c for c in raw.lower() if c in "0123456789abcdef")


def local_ipv4() -> str:
    """Best-effort local LAN address.

    Connecting a UDP socket sends no packets but makes the kernel pick the
    interface (and therefore the source address) it would route through.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never actually routed
        addr: str = sock.getsockname()[0]
        return addr
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def candidate_addresses(subnet: str = "") -> list[str]:
    """Addresses to sweep — an explicit CIDR, else the local ``/24``."""
    if subnet:
        network = ipaddress.ip_network(subnet, strict=False)
    else:
        local = local_ipv4()
        if local.startswith("127."):
            return []
        network = ipaddress.ip_network(f"{local}/24", strict=False)
    return [str(ip) for ip in network.hosts()]


async def probe_controller(
    client: httpx.AsyncClient,
    address: str,
) -> DiscoveredController | None:
    """Identify a single address, or ``None`` if it isn't a UniFi console."""
    host = f"https://{address}"
    try:
        response = await client.get(f"{host}{SYSTEM_PATH}")
    except _PROBE_ERRORS:
        return None

    if not response.is_success:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    hardware = payload.get("hardware")
    shortname = ""
    if isinstance(hardware, dict):
        shortname = str(hardware.get("shortname", ""))
    mac = normalize_mac(str(payload.get("mac", "")))
    # A UniFi console always reports both. Anything else answering /api/system
    # (a printer, a NAS) is not what we're looking for.
    if not shortname or not mac:
        return None

    return DiscoveredController(
        host=host,
        name=str(payload.get("name", "")),
        shortname=shortname,
        mac=mac,
        has_protect=await _has_protect(client, host),
    )


async def _has_protect(client: httpx.AsyncClient, host: str) -> bool:
    """Whether Protect is installed — see the module docstring on the 401."""
    try:
        response = await client.get(f"{host}{PROTECT_PROBE_PATH}")
    except _PROBE_ERRORS:
        return False
    return response.status_code in (401, 403)


@contextlib.contextmanager
def _quiet_httpx() -> Iterator[None]:
    """Mute httpx's per-request INFO line for the duration of a sweep.

    httpx logs one INFO record per request, so a /24 sweep would emit ~254
    lines every time discovery runs — replacing the log flood this feature
    exists to prevent. The sweep's own result is logged as a single line.

    This raises the level on the shared ``httpx`` logger, so a concurrent
    httpx caller loses its INFO lines for the few seconds a sweep takes.
    That's the accepted trade: sweeps are rate-limited to one per five
    minutes and only happen while the controller is unreachable.
    """
    httpx_logger = logging.getLogger("httpx")
    previous = httpx_logger.level
    if previous < logging.WARNING:
        httpx_logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        httpx_logger.setLevel(previous)


async def discover_controllers(
    *,
    subnet: str = "",
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    concurrency: int = DEFAULT_CONCURRENCY,
    client: httpx.AsyncClient | None = None,
) -> list[DiscoveredController]:
    """Sweep the network for UniFi OS consoles.

    Consoles running Protect sort first — that's what callers looking for a
    doorbell want, and it makes "take the first result" the right default.
    """
    addresses = candidate_addresses(subnet)
    if not addresses:
        return []

    owned = client is None
    probe_client = client or httpx.AsyncClient(
        verify=False,
        timeout=timeout,
        follow_redirects=True,
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def _guarded(address: str) -> DiscoveredController | None:
        async with semaphore:
            return await probe_controller(probe_client, address)

    try:
        with _quiet_httpx():
            results = await asyncio.gather(
                *(_guarded(address) for address in addresses),
                return_exceptions=True,
            )
    finally:
        if owned:
            await probe_client.aclose()

    found = [r for r in results if isinstance(r, DiscoveredController)]
    found.sort(key=lambda c: (not c.has_protect, c.host))
    if found:
        logger.info(
            "UniFi discovery found %d console(s): %s",
            len(found),
            ", ".join(c.describe() for c in found),
        )
    return found


async def find_controller(
    *,
    mac: str = "",
    require_protect: bool = True,
    subnet: str = "",
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    client: httpx.AsyncClient | None = None,
) -> DiscoveredController | None:
    """Find one console, preferring a known ``mac``.

    Re-homing by MAC is the point: when a console's DHCP lease changes we
    want *that* console at its new address, not whichever console answers
    first. With no MAC known, fall back to the first Protect console.
    """
    controllers = await discover_controllers(
        subnet=subnet, timeout=timeout, client=client
    )
    if mac:
        wanted = normalize_mac(mac)
        for controller in controllers:
            if controller.mac == wanted:
                return controller
        return None
    for controller in controllers:
        if controller.has_protect or not require_protect:
            return controller
    return None
