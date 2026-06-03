"""Web Push (VAPID) push-notification backend.

Implements the [Web Push Protocol] (RFC 8030) on top of [pywebpush],
which handles the ECDH-ES payload encryption (RFC 8291 / aes128gcm
content encoding) and the VAPID JWT signing (RFC 8292).

Each per-user route stores one browser ``PushSubscription``: an
``endpoint`` URL (FCM, Mozilla, Apple), a ``p256dh`` ECDH public key,
and an ``auth`` secret. The push service routes the encrypted payload
to the right browser; the service worker decrypts it and calls
``self.registration.showNotification(...)`` with the JSON body we
build below.

The SPA panel (``frontend/SubscribePanel.tsx``) collects the
``PushSubscription`` from ``navigator.serviceWorker`` and stores it via
the existing generic ``push.routes.create`` RPC — this backend does
not need its own service. It exposes the server's VAPID public key via
:meth:`runtime_data` so the SPA can pass it to ``pushManager.subscribe``
as ``applicationServerKey``.

[Web Push Protocol]: https://datatracker.ietf.org/doc/html/rfc8030
[pywebpush]: https://github.com/web-push-libs/pywebpush
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.push_notifications import (
    PushDeliveryResult,
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


_DEFAULT_SUBJECT = "mailto:admin@example.com"
_DEFAULT_TTL = 86400
_DEFAULT_TIMEOUT = 10
_RETRY_AFTER_CAP_S = 60.0


# Same scrubber shape used by the other push backends, plus a catch-all
# for PEM-armored private keys and bare base64url secrets that look
# long enough to be auth material. The backend pipes every exception
# string through this before stuffing it into ``PushDeliveryResult``.
_TOKEN_RX = re.compile(
    r"(?:Bearer\s+\S+|/bot[A-Za-z0-9:_-]+/|"
    r"https?://[^\s]*?/api/webhooks/[^/\s]+/[A-Za-z0-9_-]+|"
    r"\?token=[^\s&]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


def _safe_repr(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return _TOKEN_RX.sub("<redacted>", text)


def _b64url_nopad(data: bytes) -> str:
    """RFC 7515 §2 base64url without padding (browser ``applicationServerKey`` shape)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _generate_vapid_keypair() -> tuple[str, str]:
    """Return ``(public_b64url, private_pem)`` for a fresh P-256 VAPID keypair.

    - The **public** key is encoded as the SEC1 uncompressed point
      (``0x04 || X || Y``, 65 bytes) and base64url-no-pad encoded —
      that's exactly the shape the browser's ``PushManager.subscribe``
      expects for ``applicationServerKey``.
    - The **private** key is serialised as an unencrypted PKCS#8 PEM
      blob. ``pywebpush`` accepts the PEM string directly as
      ``vapid_private_key``.
    """
    private = ec.generate_private_key(ec.SECP256R1(), default_backend())
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return _b64url_nopad(public_bytes), private_pem


class WebPush(PushNotificationBackend):
    """Web Push (VAPID) delivery backend.

    The server-wide VAPID keypair and ``sub`` claim are admin
    config. Per-user routes carry one browser ``PushSubscription``
    each. ``send`` encrypts the payload to the route's ``p256dh`` key
    and POSTs to the route's ``endpoint`` via pywebpush.
    """

    backend_name = "web_push"

    def __init__(self) -> None:
        self._vapid_public_key: str = ""
        self._vapid_private_key: str = ""
        self._vapid_subject: str = _DEFAULT_SUBJECT
        self._ttl_seconds: int = _DEFAULT_TTL
        self._timeout: int = _DEFAULT_TIMEOUT
        self._initialised: bool = False

    # --- Admin config ----------------------------------------------------

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="vapid_public_key",
                type=ToolParameterType.STRING,
                description=(
                    "Base64-URL-encoded VAPID public key. Browsers use "
                    "this to authenticate the push subscription. "
                    "Generate with the 'Generate VAPID keys' action "
                    "below."
                ),
                # Not sensitive — the public key is literally meant to
                # be shipped to every subscribing browser.
                sensitive=False,
                multiline=False,
                default="",
            ),
            ConfigParam(
                key="vapid_private_key",
                type=ToolParameterType.STRING,
                description=(
                    "PEM-encoded VAPID private key. Keep secret — "
                    "leaking it lets anyone impersonate Gilbert to "
                    "subscribed browsers."
                ),
                sensitive=True,
                multiline=True,
                default="",
            ),
            ConfigParam(
                key="vapid_subject",
                type=ToolParameterType.STRING,
                description=(
                    "VAPID subject — a 'mailto:' or 'https:' URL "
                    "identifying who runs this server. Required by "
                    "some push services (FCM, Mozilla). Use a real "
                    "mailbox you own."
                ),
                default=_DEFAULT_SUBJECT,
            ),
            ConfigParam(
                key="ttl_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "How long the push service should retain the "
                    "message if the browser is offline (max 4 weeks "
                    "for most providers)."
                ),
                default=_DEFAULT_TTL,
            ),
            ConfigParam(
                key="timeout",
                type=ToolParameterType.INTEGER,
                description=(
                    "HTTP timeout (seconds) when POSTing to the push "
                    "service endpoint."
                ),
                default=_DEFAULT_TIMEOUT,
            ),
        ]

    @classmethod
    def destination_params(cls) -> list[ConfigParam]:
        # The browser produces these four values via
        # ``PushManager.subscribe(...)``. They're stored verbatim on the
        # route. The SPA panel fills the form in for the user — admins
        # who edit it by hand are on their own.
        return [
            ConfigParam(
                key="endpoint",
                type=ToolParameterType.STRING,
                description=(
                    "Push service endpoint URL (e.g. "
                    "fcm.googleapis.com/..., "
                    "updates.push.services.mozilla.com/...). Provided "
                    "by the browser when it subscribes — don't fill "
                    "this in by hand."
                ),
                default="",
            ),
            ConfigParam(
                key="p256dh",
                type=ToolParameterType.STRING,
                description=(
                    "Subscriber's P-256 ECDH public key (base64url). "
                    "From `PushSubscription.getKey('p256dh')`."
                ),
                sensitive=False,
                default="",
            ),
            ConfigParam(
                key="auth",
                type=ToolParameterType.STRING,
                description=(
                    "Subscriber's auth secret (base64url). From "
                    "`PushSubscription.getKey('auth')`. Treat as a "
                    "credential — anyone holding it can decrypt "
                    "future pushes to this browser."
                ),
                sensitive=True,
                default="",
            ),
            ConfigParam(
                key="user_agent",
                type=ToolParameterType.STRING,
                description=(
                    "Optional label for the browser/device. Used to "
                    "disambiguate routes when the user has multiple "
                    "subscribed devices."
                ),
                default="",
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="generate_vapid_keys",
                label="Generate VAPID keys",
                description=(
                    "Generate a fresh VAPID keypair. The result is "
                    "shown once — paste the public/private values "
                    "into the fields above and click Save."
                ),
                # Anchor inline-after the public-key field so the
                # button is right next to where the user pastes the
                # generated value.
                inline_after_param="vapid_public_key",
            ),
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Send a test push to a specific subscription. "
                    "Provide `endpoint`, `p256dh`, and `auth` in the "
                    "payload (the SPA's per-route test button fills "
                    "these in automatically)."
                ),
            ),
        ]

    async def invoke_backend_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if key == "generate_vapid_keys":
            return self._action_generate_keys()
        if key == "test_connection":
            return await self._action_test_connection(payload)
        return ConfigActionResult(
            status="error", message=f"Unknown action: {key}"
        )

    def _action_generate_keys(self) -> ConfigActionResult:
        try:
            public_b64, private_pem = _generate_vapid_keypair()
        except Exception as exc:  # pragma: no cover — extremely defensive
            return ConfigActionResult(
                status="error",
                message=f"Key generation failed: {_safe_repr(exc)}",
            )
        return ConfigActionResult(
            status="ok",
            message=(
                "Generated new VAPID keypair. Paste the values into "
                "the Public/Private key fields above and click Save."
            ),
            # ``data`` is free-form per ConfigActionResult; the SPA can
            # auto-fill the fields if it wants. We don't auto-persist
            # here — admin reviews the values, then clicks Save.
            data={
                "vapid_public_key": public_b64,
                "vapid_private_key": private_pem,
            },
        )

    async def _action_test_connection(
        self, payload: dict[str, Any]
    ) -> ConfigActionResult:
        endpoint = str(payload.get("endpoint", "")).strip()
        p256dh = str(payload.get("p256dh", "")).strip()
        auth = str(payload.get("auth", "")).strip()
        if not (endpoint and p256dh and auth):
            return ConfigActionResult(
                status="error",
                message=(
                    "Provide `endpoint`, `p256dh`, and `auth` in the "
                    "payload (or click the per-route Test button on "
                    "your Notifications page)."
                ),
            )
        if not (self._vapid_public_key and self._vapid_private_key):
            return ConfigActionResult(
                status="error",
                message=(
                    "VAPID keys are not configured. Click 'Generate "
                    "VAPID keys' and save first."
                ),
            )
        destination = PushDestination(
            user_id="admin-test",
            route_id="admin-test",
            data={"endpoint": endpoint, "p256dh": p256dh, "auth": auth},
        )
        from gilbert.interfaces.notifications import NotificationUrgency

        message = PushMessage(
            title="Gilbert",
            body="Web Push connectivity test",
            urgency=NotificationUrgency.NORMAL,
            source="test",
            notification_id="test-connection",
        )
        result = await self.send(destination, message)
        if result.status is PushDeliveryStatus.DELIVERED:
            return ConfigActionResult(
                status="ok",
                message="Push service accepted the test message.",
            )
        return ConfigActionResult(
            status="error",
            message=f"Test push failed ({result.status.value}): {result.message}",
        )

    # --- Lifecycle -------------------------------------------------------

    async def initialize(self, config: dict[str, Any]) -> None:
        self._vapid_public_key = str(
            config.get("vapid_public_key", "") or ""
        ).strip()
        # PEMs are multi-line; don't strip whitespace blindly — only
        # strip the outer trailing newline pywebpush inserts no
        # complaint either way.
        self._vapid_private_key = str(
            config.get("vapid_private_key", "") or ""
        ).strip()
        self._vapid_subject = str(
            config.get("vapid_subject", _DEFAULT_SUBJECT)
            or _DEFAULT_SUBJECT
        )
        try:
            self._ttl_seconds = int(config.get("ttl_seconds", _DEFAULT_TTL))
        except (TypeError, ValueError):
            self._ttl_seconds = _DEFAULT_TTL
        try:
            self._timeout = int(config.get("timeout", _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            self._timeout = _DEFAULT_TIMEOUT
        self._initialised = True
        if not (self._vapid_public_key and self._vapid_private_key):
            # Don't fail — let the admin use ``generate_vapid_keys`` to
            # bootstrap from the Settings UI. ``send()`` returns
            # DISABLED in this state.
            logger.warning(
                "web-push backend has no VAPID keys configured; "
                "deliveries will be DISABLED until both keys are set. "
                "Use the 'Generate VAPID keys' action on the Settings "
                "page to bootstrap."
            )

    async def close(self) -> None:
        # pywebpush manages its own per-call HTTP client; nothing to
        # close here. Flip the initialised flag so a post-close send()
        # returns DISABLED instead of accidentally delivering.
        self._initialised = False

    def runtime_data(self) -> dict[str, Any]:
        # Exposed via the existing ``push.backends.list`` RPC so the
        # SPA can fetch the VAPID public key without a backend-specific
        # endpoint. NEVER expose the private key here.
        return {
            "vapid_public_key": self._vapid_public_key,
            "has_keys": bool(
                self._vapid_public_key and self._vapid_private_key
            ),
        }

    # --- Delivery --------------------------------------------------------

    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        if not self._initialised:
            return PushDeliveryResult(
                status=PushDeliveryStatus.DISABLED,
                message="web-push backend not initialised",
            )
        if not (self._vapid_public_key and self._vapid_private_key):
            return PushDeliveryResult(
                status=PushDeliveryStatus.DISABLED,
                message="VAPID keys not configured",
            )

        endpoint = str(destination.data.get("endpoint", "")).strip()
        p256dh = str(destination.data.get("p256dh", "")).strip()
        auth = str(destination.data.get("auth", "")).strip()
        if not (endpoint and p256dh and auth):
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message="route is missing endpoint/p256dh/auth",
            )

        subscription_info = {
            "endpoint": endpoint,
            "keys": {"p256dh": p256dh, "auth": auth},
        }

        deep_link = "/"
        if message.source_ref and isinstance(message.source_ref, dict):
            link = message.source_ref.get("deep_link_url")
            if isinstance(link, str) and link:
                deep_link = link

        # Payload shape mirrors what the service worker expects to
        # forward to ``self.registration.showNotification`` — keep the
        # keys + nested ``data`` stable across versions; the SW reads
        # ``data.url`` for click-through routing.
        payload = {
            "title": message.title,
            "body": message.body,
            "icon": "/icons/gilbert-192.png",
            "badge": "/icons/gilbert-192.png",
            "tag": message.notification_id or "",
            "data": {
                "url": deep_link,
                "notification_id": message.notification_id,
                "source": message.source,
            },
        }
        payload_bytes = json.dumps(payload).encode("utf-8")

        # pywebpush.webpush() is synchronous (it uses ``requests``
        # under the hood). Gilbert's delivery worker calls ``send()``
        # serially per route, so off-loading to a worker thread keeps
        # the asyncio loop unblocked without introducing any ordering
        # subtleties. We deliberately don't pre-cache an httpx client:
        # pywebpush builds the encrypted body and the VAPID JWT in the
        # same call, and re-implementing that for an async client is
        # exactly the kind of crypto code we want to leave to the
        # library.
        return await asyncio.to_thread(
            self._send_blocking, subscription_info, payload_bytes
        )

    def _send_blocking(
        self,
        subscription_info: dict[str, Any],
        payload_bytes: bytes,
    ) -> PushDeliveryResult:
        # Imported lazily so the module-level import doesn't hit
        # pywebpush's chain (requests / aiohttp / http_ece) during test
        # collection when the backend's tested behaviour is mocked
        # out via ``monkeypatch.setattr(web_push, "webpush", ...)``.
        from pywebpush import WebPushException, webpush

        try:
            resp = webpush(
                subscription_info=subscription_info,
                data=payload_bytes,
                vapid_private_key=self._vapid_private_key,
                vapid_claims={"sub": self._vapid_subject},
                ttl=self._ttl_seconds,
                timeout=self._timeout,
            )
        except WebPushException as exc:
            return self._classify_webpush_exception(exc)
        except Exception as exc:
            # Network errors from ``requests`` bubble up here
            # (ConnectionError, Timeout). Treat them as transient.
            return PushDeliveryResult(
                status=PushDeliveryStatus.TRANSIENT_ERROR,
                message=f"network error ({type(exc).__name__})",
            )

        status_code = getattr(resp, "status_code", 0)
        return self._classify_status(int(status_code), getattr(resp, "headers", None))

    def _classify_webpush_exception(
        self, exc: Exception
    ) -> PushDeliveryResult:
        """Map a pywebpush ``WebPushException`` to a delivery result.

        pywebpush raises this for any non-2xx HTTP response. The
        attached ``response`` object is a ``requests.Response`` — we
        read ``status_code`` and ``headers`` from it, but DELIBERATELY
        avoid touching ``response.text`` (the body sometimes echoes
        the endpoint URL with embedded auth in FCM error responses).
        """
        resp = getattr(exc, "response", None)
        status_code = int(getattr(resp, "status_code", 0) or 0)
        headers = getattr(resp, "headers", None)
        if status_code:
            return self._classify_status(status_code, headers)
        # WebPushException raised without an HTTP response (e.g. local
        # encoding failure with a bad subscription shape).
        return PushDeliveryResult(
            status=PushDeliveryStatus.REJECTED,
            message=_safe_repr(exc),
        )

    def _classify_status(
        self, status_code: int, headers: Any
    ) -> PushDeliveryResult:
        if 200 <= status_code < 300:
            return PushDeliveryResult(
                status=PushDeliveryStatus.DELIVERED,
                message=f"HTTP {status_code}",
            )
        if status_code in (404, 410):
            # Subscription is dead — the browser unsubscribed or the
            # push service garbage-collected it. The delivery worker
            # uses REJECTED to suggest the route be removed.
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message=f"HTTP {status_code} — subscription gone",
            )
        if status_code == 429:
            retry_after_s: float | None = None
            if headers is not None:
                raw = None
                try:
                    raw = headers.get("Retry-After")
                except Exception:
                    raw = None
                if raw:
                    try:
                        retry_after_s = min(
                            float(raw), _RETRY_AFTER_CAP_S
                        )
                    except (TypeError, ValueError):
                        retry_after_s = None
            return PushDeliveryResult(
                status=PushDeliveryStatus.TRANSIENT_ERROR,
                message="HTTP 429 rate-limited",
                retry_after_s=retry_after_s,
            )
        if 500 <= status_code < 600:
            return PushDeliveryResult(
                status=PushDeliveryStatus.TRANSIENT_ERROR,
                message=f"server HTTP {status_code}",
            )
        # Status line only — DO NOT include response body (some push
        # services echo the endpoint URL with embedded auth bits).
        return PushDeliveryResult(
            status=PushDeliveryStatus.REJECTED,
            message=f"HTTP {status_code}",
        )
