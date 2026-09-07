"""UniFi doorbell backend — Protect doorbell rings + Access intercom presses."""

import asyncio
import logging
import time

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.doorbell import DoorbellBackend, RingEvent
from gilbert.interfaces.tools import ToolParameterType

from .access import UniFiAccess
from .client import (
    UniFiAPIError,
    UniFiAuthError,
    UniFiClient,
    UniFiConnectionError,
)
from .discovery import discover_controllers, find_controller
from .protect import UniFiProtect

logger = logging.getLogger(__name__)

# Don't sweep the network on every failed poll. Polling runs every few
# seconds; a console that moved stays moved, so one attempt per cooldown is
# plenty and keeps discovery off the hot path.
_REDISCOVER_COOLDOWN_SECONDS = 300.0

# A dead controller fails identically forever. Logging every poll turned one
# unreachable host into ~17k warning lines a day (it produced 1.9M lines and
# most of a 780MB log file before anyone noticed). Log a repeat only when the
# message changes or the window elapses.
_REPEAT_WARNING_WINDOW_SECONDS = 900.0


class UniFiProtectDoorbellBackend(DoorbellBackend):
    """Detects entry events from UniFi Protect doorbells and Access readers."""

    backend_name = "unifi"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="host",
                type=ToolParameterType.STRING,
                description="UniFi Protect controller URL.",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="username",
                type=ToolParameterType.STRING,
                description="UniFi Protect username.",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="password",
                type=ToolParameterType.STRING,
                description="UniFi Protect password.",
                default="",
                restart_required=True,
                sensitive=True,
            ),
            ConfigParam(
                key="doorbell_names",
                type=ToolParameterType.ARRAY,
                description="Doorbells to monitor (empty = all).",
                default=[],
                choices_from="doorbells",
            ),
            ConfigParam(
                key="auto_discover",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Find the UniFi console on the local network when the "
                    "host is blank or stops responding (e.g. after its DHCP "
                    "lease changes)."
                ),
                default=True,
            ),
            ConfigParam(
                key="console_mac",
                type=ToolParameterType.STRING,
                description=(
                    "Optional console MAC. When set, auto-discovery re-homes "
                    "to this exact console instead of the first one it finds "
                    "running Protect. Run 'Discover controllers' to get it."
                ),
                default="",
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Verify UniFi Protect credentials by attempting a "
                    "login and listing doorbell cameras."
                ),
            ),
            ConfigAction(
                key="discover",
                label="Discover controllers",
                description=(
                    "Scan the local network for UniFi consoles and report "
                    "which ones run Protect, with the address and MAC to "
                    "configure."
                ),
            ),
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict,
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        if key == "discover":
            return await self._action_discover()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_discover(self) -> ConfigActionResult:
        """Report every UniFi console on the LAN, Protect ones first."""
        try:
            controllers = await discover_controllers()
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Discovery failed: {exc}",
            )

        if not controllers:
            return ConfigActionResult(
                status="error",
                message=(
                    "No UniFi consoles found on the local network. If the "
                    "console is on another subnet, set the host manually."
                ),
            )

        lines = [c.describe() for c in controllers]
        protect = [c for c in controllers if c.has_protect]
        if protect:
            best = protect[0]
            lines.append(
                f"Set host to {best.host} (console_mac {best.mac}) for Protect."
            )
        else:
            lines.append("None of these have UniFi Protect installed.")

        return ConfigActionResult(
            status="ok" if protect else "error",
            message=" ".join(lines),
            data={
                "controllers": [
                    {
                        "host": c.host,
                        "name": c.name,
                        "shortname": c.shortname,
                        "mac": c.mac,
                        "has_protect": c.has_protect,
                    }
                    for c in controllers
                ]
            },
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        """Verify the backend by calling the same method runtime polling uses.

        Intentionally does NOT call ``client.login()`` — ``UniFiClient``
        auto-logs-in on the first request (and on any 401), and that's
        the code path normal doorbell polling exercises. Calling login
        explicitly would test a different thing than what the real
        polling does, and mis-diagnose a live service as broken.
        """
        if self._client is None or self._protect is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "UniFi doorbell backend is not initialized — set host "
                    "and credentials, then save and restart."
                ),
            )
        cameras = []
        camera_err: Exception | None = None
        try:
            cameras = await self._protect.list_cameras()
        except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
            camera_err = exc
        except Exception as exc:
            camera_err = exc

        access_doors: list = []
        access_err: Exception | None = None
        if self._access is not None:
            try:
                access_doors = await self._access.list_doors()
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                access_err = exc
            except Exception as exc:
                access_err = exc

        if camera_err and (access_err or self._access is None):
            return ConfigActionResult(
                status="error",
                message=f"UniFi Protect error: {camera_err}",
            )

        doorbell_count = sum(1 for c in cameras if c.is_doorbell)
        parts = [
            f"{len(cameras)} camera(s)",
            f"{doorbell_count} doorbell(s)",
            f"{len(access_doors)} Access door(s)",
        ]
        message = "Connected to UniFi. " + ", ".join(parts) + "."
        if camera_err:
            message += f" Protect error: {camera_err}."
        if access_err:
            message += f" Access error: {access_err}."
        return ConfigActionResult(
            status="ok" if not (camera_err or access_err) else "error",
            message=message,
        )

    def __init__(self) -> None:
        self._client: UniFiClient | None = None
        self._protect: UniFiProtect | None = None
        self._access: UniFiAccess | None = None
        self._auto_discover = True
        self._console_mac = ""
        self._last_rediscover = float("-inf")
        self._warned: dict[tuple[str, str], float] = {}

    async def initialize(self, config: dict[str, object]) -> None:
        host = str(config.get("host", "") or "")
        self._auto_discover = bool(config.get("auto_discover", True))
        self._console_mac = str(config.get("console_mac", "") or "")

        username = str(config.get("username", ""))
        password = str(config.get("password", ""))
        if not username or not password:
            logger.warning("UniFi doorbell backend: no credentials configured")
            return

        if not host:
            if not self._auto_discover:
                logger.warning("UniFi doorbell backend: no host configured")
                return
            found = await self._discover_console()
            if found is None:
                logger.warning(
                    "UniFi doorbell backend: no host configured and no "
                    "console found on the local network"
                )
                return
            host = found
            logger.info("UniFi doorbell backend auto-discovered console at %s", host)

        self._client = UniFiClient(host, username, password)
        self._protect = UniFiProtect(self._client)
        self._access = UniFiAccess(self._client)
        logger.info("UniFi doorbell backend initialized (%s)", host)

    async def _discover_console(self) -> str | None:
        """Locate the console, preferring the configured MAC."""
        try:
            controller = await find_controller(mac=self._console_mac)
        except Exception as exc:
            logger.debug("UniFi discovery failed: %s", exc, exc_info=True)
            return None
        if controller is None and self._console_mac:
            # The pinned console isn't answering. Don't silently fall back to
            # a different one — that would point the doorbell at the wrong
            # hardware, which is worse than staying broken and visible.
            logger.warning(
                "UniFi console with MAC %s not found on the local network",
                self._console_mac,
            )
            return None
        return controller.host if controller else None

    async def _rehome_if_moved(self) -> bool:
        """Re-point the client after a connection failure. True if moved.

        Rate-limited: polling failures repeat every few seconds, and a
        network sweep per failure would be worse than the outage.
        """
        if not self._auto_discover or self._client is None:
            return False
        now = time.monotonic()
        if now - self._last_rediscover < _REDISCOVER_COOLDOWN_SECONDS:
            return False
        self._last_rediscover = now

        host = await self._discover_console()
        if host is None or host == self._client.host:
            return False
        logger.warning(
            "UniFi console moved from %s to %s — re-homing",
            self._client.host,
            host,
        )
        self._client.set_host(host)
        return True

    def _warn_once(self, key: str, message: str, *args: object) -> None:
        """Log a repeating failure at WARNING, then throttle it.

        The same unreachable host fails identically on every poll; without
        this a single dead device buries every other log line.
        """
        rendered = message % args if args else message
        now = time.monotonic()
        slot = (key, rendered)
        last = self._warned.get(slot)
        if last is not None and now - last < _REPEAT_WARNING_WINDOW_SECONDS:
            logger.debug("%s (repeat suppressed)", rendered)
            return
        if last is None:
            logger.warning("%s", rendered)
        else:
            logger.warning(
                "%s (still failing after %.0f minutes)",
                rendered,
                (now - last) / 60.0,
            )
        self._warned[slot] = now
        # Bound the dict — error text varies (timeouts embed addresses), and
        # this backend lives for the process lifetime.
        if len(self._warned) > 32:
            oldest = min(self._warned, key=lambda k: self._warned[k])
            del self._warned[oldest]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._protect = None
        self._access = None

    async def list_doorbell_names(self) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()

        if self._protect is not None:
            try:
                cameras = await self._protect.list_cameras()
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                self._warn_once(
                    "protect_list", "UniFi Protect doorbell list unavailable: %s", exc
                )
            else:
                for c in cameras:
                    if c.is_doorbell and c.name and c.name.lower() not in seen:
                        names.append(c.name)
                        seen.add(c.name.lower())

        if self._access is not None:
            try:
                doors = await self._access.list_doors()
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                self._warn_once(
                    "access_list", "UniFi Access door list unavailable: %s", exc
                )
            else:
                for d in doors:
                    if d.name and d.name.lower() not in seen:
                        names.append(d.name)
                        seen.add(d.name.lower())

        return names

    async def get_ring_events(self, lookback_seconds: int = 10) -> list[RingEvent]:
        if self._protect is None and self._access is None:
            return []

        async def _protect_rings() -> list[RingEvent]:
            if self._protect is None:
                return []
            lookback_minutes = max(1, (lookback_seconds // 60) + 1)
            try:
                events = await self._protect.get_detection_events(
                    lookback_minutes=lookback_minutes,
                    event_types=["ring"],
                )
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                self._warn_once(
                    "protect_ring", "UniFi Protect ring poll failed: %s", exc
                )
                if isinstance(exc, UniFiConnectionError):
                    await self._rehome_if_moved()
                return []
            return [RingEvent(camera_name=e.camera_name, timestamp=e.start) for e in events]

        async def _access_rings() -> list[RingEvent]:
            if self._access is None:
                return []
            try:
                events = await self._access.get_doorbell_events(
                    lookback_seconds=lookback_seconds,
                )
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                self._warn_once(
                    "access_ring", "UniFi Access ring poll failed: %s", exc
                )
                if isinstance(exc, UniFiConnectionError):
                    await self._rehome_if_moved()
                return []
            return [RingEvent(camera_name=e.door_name, timestamp=e.timestamp) for e in events]

        protect_rings, access_rings = await asyncio.gather(
            _protect_rings(),
            _access_rings(),
        )
        return [*protect_rings, *access_rings]
