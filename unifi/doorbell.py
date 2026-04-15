"""UniFi Protect doorbell backend — detects ring events via the Protect API."""

import logging

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.doorbell import DoorbellBackend, RingEvent
from gilbert.interfaces.tools import ToolParameterType

from .client import (
    UniFiAPIError,
    UniFiAuthError,
    UniFiClient,
    UniFiConnectionError,
)
from .protect import UniFiProtect

logger = logging.getLogger(__name__)


class UniFiProtectDoorbellBackend(DoorbellBackend):
    """Detects doorbell rings via UniFi Protect."""

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
                sensitive=True,
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
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict,
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
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
        try:
            cameras = await self._protect.list_cameras()
        except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
            return ConfigActionResult(
                status="error",
                message=f"UniFi Protect error: {exc}",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        doorbell_count = sum(1 for c in cameras if c.is_doorbell)
        return ConfigActionResult(
            status="ok",
            message=(
                f"Connected to UniFi Protect. Found {len(cameras)} "
                f"camera(s), {doorbell_count} doorbell(s)."
            ),
        )

    def __init__(self) -> None:
        self._client: UniFiClient | None = None
        self._protect: UniFiProtect | None = None

    async def initialize(self, config: dict[str, object]) -> None:
        host = config.get("host")
        if not host:
            logger.warning("UniFi doorbell backend: no host configured")
            return

        username = str(config.get("username", ""))
        password = str(config.get("password", ""))
        if not username or not password:
            logger.warning("UniFi doorbell backend: no credentials configured")
            return

        self._client = UniFiClient(str(host), username, password)
        self._protect = UniFiProtect(self._client)
        logger.info("UniFi doorbell backend initialized (%s)", host)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._protect = None

    async def list_doorbell_names(self) -> list[str]:
        if self._protect is None:
            return []
        cameras = await self._protect.list_cameras()
        return [c.name for c in cameras if c.is_doorbell]

    async def get_ring_events(self, lookback_seconds: int = 10) -> list[RingEvent]:
        if self._protect is None:
            return []

        lookback_minutes = max(1, (lookback_seconds // 60) + 1)
        events = await self._protect.get_detection_events(
            lookback_minutes=lookback_minutes,
            event_types=["ring"],
        )

        return [
            RingEvent(
                camera_name=e.camera_name,
                timestamp=e.start,
            )
            for e in events
        ]
