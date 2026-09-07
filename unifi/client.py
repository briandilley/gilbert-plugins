"""UniFi OS HTTP client — async client with cookie-based auth and auto re-login."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class UniFiAuthError(Exception):
    """Authentication failed (bad credentials or locked out)."""


class UniFiConnectionError(Exception):
    """Cannot reach the controller."""


class UniFiAPIError(Exception):
    """Unexpected API response."""


class UniFiClient:
    """Async HTTP client for UniFi OS controllers (UDM, UDR, UNVR, CK Gen2+).

    Handles cookie-based session auth with automatic re-login on 401.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self._host = self._normalize_host(host)
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(
            base_url=self._host,
            verify=verify_ssl,
            timeout=timeout,
            follow_redirects=True,
        )
        self._logged_in = False

    @staticmethod
    def _normalize_host(host: str) -> str:
        """Force the https scheme UniFi OS requires.

        UniFi OS only serves its API over HTTPS — HTTP requests get a 301 to
        the https URL, which httpx may downgrade to GET, breaking login. Force
        the upgrade up front so users can paste either scheme.
        """
        normalized = host.rstrip("/")
        if normalized.startswith("http://"):
            normalized = "https://" + normalized[len("http://") :]
        elif not normalized.startswith("https://"):
            normalized = "https://" + normalized
        return normalized

    @property
    def host(self) -> str:
        return self._host

    def set_host(self, host: str) -> None:
        """Re-point this client at a new address, dropping the session.

        Used when a console's DHCP lease changes: discovery finds the same
        hardware at a new address and we re-home rather than failing every
        poll until someone edits the config by hand.
        """
        normalized = self._normalize_host(host)
        if normalized == self._host:
            return
        self._host = normalized
        self._client.base_url = httpx.URL(normalized)
        # The old console's cookie is meaningless on the new address.
        self._client.cookies.clear()
        self._logged_in = False
        logger.info("UniFi client re-homed to %s", normalized)

    async def login(self) -> None:
        """Authenticate and store the session cookie."""
        try:
            response = await self._client.post(
                "/api/auth/login",
                json={"username": self._username, "password": self._password},
            )
        except httpx.ConnectError as e:
            raise UniFiConnectionError(f"Cannot reach {self._host}: {e}") from e

        if response.status_code == 401 or response.status_code == 403:
            raise UniFiAuthError(f"Authentication failed for {self._host}")

        if not response.is_success:
            raise UniFiAPIError(
                f"Login failed with status {response.status_code}: {response.text[:200]}"
            )

        self._logged_in = True
        logger.debug("Logged in to %s", self._host)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET request. Returns parsed JSON or None on 404."""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        """Authenticated POST request. Returns parsed JSON."""
        return await self._request("POST", path, json=data)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an authenticated request with auto re-login on 401."""
        if not self._logged_in:
            await self.login()

        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.ConnectError as e:
            raise UniFiConnectionError(f"Cannot reach {self._host}: {e}") from e

        # Auto re-login on session expiry
        if response.status_code == 401:
            logger.debug("Session expired on %s, re-authenticating", self._host)
            await self.login()
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.ConnectError as e:
                raise UniFiConnectionError(f"Cannot reach {self._host}: {e}") from e

        if response.status_code == 404:
            return None

        if not response.is_success:
            raise UniFiAPIError(
                f"{method} {path} returned {response.status_code}: {response.text[:200]}"
            )

        # Some endpoints return empty body on success
        if not response.content:
            return {}

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type and "text/json" not in content_type:
            raise UniFiAPIError(f"{method} {path} returned non-JSON content-type: {content_type}")

        return response.json()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
        self._logged_in = False
