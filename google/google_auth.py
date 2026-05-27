"""Google OAuth authentication backend.

Authenticates users via Google OAuth 2.0 ID token verification.
Self-contained — reads OAuth client credentials from auth config.
"""

import logging
import urllib.parse
from typing import Any

from gilbert.interfaces.auth import (
    AuthBackend,
    AuthInfo,
    LoginMethod,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


class GoogleAuthBackend(AuthBackend):
    """Authenticates users via Google OAuth ID tokens."""

    backend_name = "google"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="client_id",
                type=ToolParameterType.STRING,
                description="Google OAuth client ID.",
                restart_required=True,
                sensitive=True,
            ),
            ConfigParam(
                key="client_secret",
                type=ToolParameterType.STRING,
                description="Google OAuth client secret.",
                restart_required=True,
                sensitive=True,
            ),
            ConfigParam(
                key="domain",
                type=ToolParameterType.STRING,
                description="Restrict Google login to this domain (empty = any).",
                default="",
            ),
            ConfigParam(
                key="callback_url_source",
                type=ToolParameterType.STRING,
                description=(
                    "Where to send Google's OAuth redirect. "
                    "'tunnel' = public tunnel URL (reachable from anywhere). "
                    "'internal_url' = LAN hostname via sslip.io (the browser "
                    "must trust Gilbert's TLS cert). "
                    "'request' = the origin the login request arrived on "
                    "(only works if the browser already reached Gilbert on a "
                    "hostname Google accepts). Whichever you pick, add the "
                    "matching /auth/login/google/callback URL to your Google "
                    "Cloud OAuth client's authorized redirect URIs."
                ),
                default="tunnel",
                restart_required=True,
                choices=("tunnel", "internal_url", "request"),
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test configuration",
                description=(
                    "Verify that a Google OAuth client ID and client "
                    "secret are present in the backend configuration."
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
        if not self._oauth_client_id:
            return ConfigActionResult(
                status="error",
                message="Google OAuth client ID is missing.",
            )
        if not self._client_secret:
            return ConfigActionResult(
                status="error",
                message="Google OAuth client secret is missing.",
            )
        domain_msg = f"restricted to {self._domain}" if self._domain else "any domain"
        return ConfigActionResult(
            status="ok",
            message=(
                f"Google OAuth credentials configured ({domain_msg}). "
                "Actual sign-in is verified on first user login."
            ),
        )

    def __init__(self) -> None:
        self._oauth_client_id: str = ""
        self._client_secret: str = ""
        self._domain: str = ""
        self._tunnel: Any = None
        self._internal_url: Any = None
        self._callback_url_source: str = "tunnel"
        self._warned_fallback: bool = False

    @property
    def provider_type(self) -> str:
        return "google"

    async def initialize(self, config: dict[str, Any]) -> None:
        self._oauth_client_id = config.get("client_id", "")
        self._client_secret = config.get("client_secret", "")
        self._domain = config.get("domain", "")
        self._callback_url_source = config.get("callback_url_source", "tunnel")

        if not self._oauth_client_id:
            logger.warning("Google OAuth client ID not configured")
        else:
            logger.info("Google auth backend initialized (domain=%s)", self._domain or "(any)")

    async def close(self) -> None:
        pass

    def set_tunnel(self, tunnel: Any) -> None:
        """Set the tunnel service for public HTTPS callback URLs."""
        self._tunnel = tunnel

    def set_internal_url(self, provider: Any) -> None:
        """Set the internal-URL provider for LAN-only HTTPS callback URLs."""
        self._internal_url = provider

    def get_login_method(self) -> LoginMethod:
        return LoginMethod(
            provider_type="google",
            display_name="Sign in with Google",
            method="redirect",
            redirect_url="/auth/login/google/start",
        )

    def get_callback_url(self, request_base_url: str = "") -> str:
        path = "/auth/login/google/callback"
        src = self._callback_url_source

        chosen = ""
        if src == "tunnel" and self._tunnel is not None and self._tunnel.public_url:
            chosen = self._tunnel.public_url_for(path)
        elif (
            src == "internal_url"
            and self._internal_url is not None
            and self._internal_url.internal_url
        ):
            chosen = self._internal_url.internal_url_for(path)

        if chosen:
            return chosen

        # 'request' source, or the chosen source isn't available: use the
        # origin the browser reached Gilbert on. Warn once when this is an
        # unintended fallback so a misconfiguration is visible.
        if src != "request" and not self._warned_fallback:
            logger.warning(
                "Google callback_url_source=%r is unavailable — falling back "
                "to the request origin. Enable the matching service or switch "
                "the source in Google auth settings.",
                src,
            )
            self._warned_fallback = True

        base = request_base_url.rstrip("/") if request_base_url else ""
        return f"{base}{path}"

    def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Build the full Google OAuth authorization URL."""
        params = urllib.parse.urlencode(
            {
                "client_id": self._oauth_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "access_type": "offline",
                "prompt": "select_account",
                "state": state,
                **({"hd": self._domain} if self._domain else {}),
            }
        )
        return f"https://accounts.google.com/o/oauth2/v2/auth?{params}"

    async def authenticate(self, credentials: dict[str, Any]) -> AuthInfo | None:
        id_token_str = credentials.get("id_token", "")
        if not id_token_str:
            return None

        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token

            if not self._oauth_client_id:
                logger.error("Google OAuth client ID not configured")
                return None

            info = id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                self._oauth_client_id,
            )

            email = info.get("email", "")
            if not email:
                logger.warning("Google ID token missing email claim")
                return None

            if self._domain and info.get("hd") != self._domain:
                logger.warning(
                    "Google auth rejected: domain %s != %s", info.get("hd"), self._domain
                )
                return None

            return AuthInfo(
                provider_type="google",
                provider_user_id=info.get("sub", ""),
                email=email,
                display_name=info.get("name", email.split("@")[0]),
                raw={"picture": info.get("picture", "")},
            )
        except Exception:
            logger.exception("Google OAuth token verification failed")
            return None

    async def handle_callback(self, params: dict[str, Any]) -> AuthInfo | None:
        """Exchange OAuth authorization code for ID token and authenticate."""
        code = params.get("code", "")
        redirect_uri = params.get("redirect_uri", "")
        if not code or not self._oauth_client_id:
            return None

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "code": code,
                        "client_id": self._oauth_client_id,
                        "client_secret": self._client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
                resp.raise_for_status()
                token_data = resp.json()

            id_token_str = token_data.get("id_token", "")
            if not id_token_str:
                logger.warning("Google OAuth token exchange returned no id_token")
                return None

            return await self.authenticate({"id_token": id_token_str})
        except Exception:
            logger.exception("Google OAuth callback failed")
            return None

    @property
    def oauth_client_id(self) -> str:
        return self._oauth_client_id

    @property
    def client_secret(self) -> str:
        return self._client_secret

    @property
    def domain(self) -> str:
        return self._domain
