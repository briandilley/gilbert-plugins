"""Unit tests for the web-push backend."""

from __future__ import annotations

import base64
import sys
import types
from typing import Any

import pytest
from gilbert_plugin_web_push.web_push import (
    WebPush,
    _generate_vapid_keypair,
    _safe_repr,
)

from gilbert.interfaces.notifications import NotificationUrgency
from gilbert.interfaces.push_notifications import (
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────


_FAKE_PUBLIC = "BLAHbase64urlPublicKey"
# A valid-looking but obviously-fake PEM. We need a string whose
# presence in error output we can grep against to verify scrubbing.
_FAKE_PRIVATE_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg/leakthis/secret\n"
    "-----END PRIVATE KEY-----\n"
)
_FAKE_AUTH_SECRET = "SUPERSECRETAUTHTOKEN42"


def _make_message(
    *,
    urgency: NotificationUrgency = NotificationUrgency.NORMAL,
    source: str = "agent",
    deep_link: str | None = None,
) -> PushMessage:
    return PushMessage(
        title="Gilbert",
        body="hello",
        urgency=urgency,
        source=source,
        notification_id="n_1",
        source_ref=(
            {"deep_link_url": deep_link} if deep_link is not None else None
        ),
    )


def _make_destination(
    *,
    endpoint: str = "https://fcm.googleapis.com/fcm/send/abc",
    p256dh: str = "BPublicKeyOfTheBrowser",
    auth: str = _FAKE_AUTH_SECRET,
) -> PushDestination:
    return PushDestination(
        user_id="u_1",
        route_id="r_1",
        data={
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": "Mozilla/5.0 (Test)",
        },
    )


# ── Helpers: shim a fake ``pywebpush`` module into ``sys.modules`` ────
#
# ``web_push._send_blocking`` does ``from pywebpush import …`` lazily.
# Rather than depending on the real package being installed in the
# test venv, we install a stub module that exposes a controllable
# ``webpush`` callable plus a ``WebPushException`` class with the
# same attribute shape pywebpush uses (``message`` + ``response``).


class _FakeResponse:
    """Stand-in for ``requests.Response`` — only the attributes
    ``WebPush`` reads after a send."""

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class _FakeWebPushException(Exception):  # noqa: N818 — mirrors pywebpush.WebPushException's name
    def __init__(
        self, message: str, response: _FakeResponse | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.response = response


@pytest.fixture
def fake_pywebpush(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a stub ``pywebpush`` module and return a dict the test
    can poke (``handler`` callable, captured ``calls`` list)."""

    state: dict[str, Any] = {"calls": [], "handler": None}

    def webpush(*args: Any, **kwargs: Any) -> Any:
        state["calls"].append(kwargs)
        handler = state["handler"]
        if handler is None:
            return _FakeResponse(201)
        return handler(**kwargs)

    fake_mod = types.ModuleType("pywebpush")
    fake_mod.webpush = webpush  # type: ignore[attr-defined]
    fake_mod.WebPushException = _FakeWebPushException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywebpush", fake_mod)
    return state


# ── Registration / shape ──────────────────────────────────────────────


async def test_web_push_registered_in_backend_registry() -> None:
    assert "web_push" in PushNotificationBackend.registered_backends()
    assert (
        PushNotificationBackend.registered_backends()["web_push"] is WebPush
    )


async def test_destination_params_declares_expected_keys() -> None:
    keys = {p.key for p in WebPush.destination_params()}
    assert keys == {"endpoint", "p256dh", "auth", "user_agent"}


async def test_destination_params_marks_auth_sensitive() -> None:
    by_key = {p.key: p for p in WebPush.destination_params()}
    assert by_key["auth"].sensitive is True
    # The endpoint and p256dh are public-by-design; user_agent is
    # display-only. None of those should be flagged sensitive.
    assert by_key["endpoint"].sensitive is False
    assert by_key["p256dh"].sensitive is False
    assert by_key["user_agent"].sensitive is False


async def test_backend_config_params_declares_expected_keys() -> None:
    keys = {p.key for p in WebPush.backend_config_params()}
    assert keys == {
        "vapid_public_key",
        "vapid_private_key",
        "vapid_subject",
        "ttl_seconds",
        "timeout",
    }


async def test_backend_config_params_sensitivity_flags() -> None:
    by_key = {p.key: p for p in WebPush.backend_config_params()}
    # Public key is meant to be shipped to browsers — NOT sensitive.
    assert by_key["vapid_public_key"].sensitive is False
    # Private key obviously is.
    assert by_key["vapid_private_key"].sensitive is True
    assert by_key["vapid_private_key"].multiline is True
    # Subject / TTL / timeout are non-secret tunables.
    assert by_key["vapid_subject"].sensitive is False
    assert by_key["ttl_seconds"].sensitive is False
    assert by_key["timeout"].sensitive is False


async def test_backend_actions_includes_generate_and_test() -> None:
    keys = {a.key for a in WebPush.backend_actions()}
    assert "generate_vapid_keys" in keys
    assert "test_connection" in keys


# ── generate_vapid_keys action ───────────────────────────────────────


async def test_generate_vapid_keys_returns_valid_looking_keypair() -> None:
    backend = WebPush()
    result = await backend.invoke_backend_action("generate_vapid_keys", {})
    assert result.status == "ok"
    public = result.data["vapid_public_key"]
    private = result.data["vapid_private_key"]
    assert isinstance(public, str) and public
    assert isinstance(private, str) and private
    # Public should be base64url-no-pad (no padding, only urlsafe alphabet).
    assert "=" not in public
    decoded = base64.urlsafe_b64decode(public + "==")
    # SEC1 uncompressed P-256 point is exactly 65 bytes (0x04 || X || Y).
    assert len(decoded) == 65
    assert decoded[0] == 0x04
    # Private should be a PKCS#8 PEM blob.
    assert "BEGIN PRIVATE KEY" in private
    assert "END PRIVATE KEY" in private


async def test_generate_vapid_keypair_helper_round_trips_uniquely() -> None:
    a_pub, a_priv = _generate_vapid_keypair()
    b_pub, b_priv = _generate_vapid_keypair()
    # Independent generations should not collide (overwhelmingly).
    assert a_pub != b_pub
    assert a_priv != b_priv


# ── send() — REJECTED / DISABLED branches ────────────────────────────


async def test_send_returns_disabled_when_uninitialised() -> None:
    backend = WebPush()
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.DISABLED


async def test_send_returns_disabled_when_vapid_unconfigured() -> None:
    backend = WebPush()
    await backend.initialize({})  # both keys empty
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.DISABLED
    assert "VAPID" in result.message


async def test_send_with_missing_endpoint_is_rejected(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = WebPush()
    await backend.initialize(
        {
            "vapid_public_key": _FAKE_PUBLIC,
            "vapid_private_key": _FAKE_PRIVATE_PEM,
        }
    )
    dest = PushDestination(
        user_id="u_1",
        route_id="r_1",
        data={"endpoint": "", "p256dh": "x", "auth": "y"},
    )
    result = await backend.send(dest, _make_message())
    assert result.status is PushDeliveryStatus.REJECTED
    assert "endpoint" in result.message
    # Must not have called the push service.
    assert fake_pywebpush["calls"] == []


async def test_send_with_missing_p256dh_is_rejected(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = WebPush()
    await backend.initialize(
        {
            "vapid_public_key": _FAKE_PUBLIC,
            "vapid_private_key": _FAKE_PRIVATE_PEM,
        }
    )
    dest = PushDestination(
        user_id="u_1",
        route_id="r_1",
        data={
            "endpoint": "https://fcm.example/x",
            "p256dh": "",
            "auth": "y",
        },
    )
    result = await backend.send(dest, _make_message())
    assert result.status is PushDeliveryStatus.REJECTED
    assert fake_pywebpush["calls"] == []


# ── send() — happy path + status code mapping ────────────────────────


async def _initialised_backend() -> WebPush:
    backend = WebPush()
    await backend.initialize(
        {
            "vapid_public_key": _FAKE_PUBLIC,
            "vapid_private_key": _FAKE_PRIVATE_PEM,
            "vapid_subject": "mailto:gilbert@example.test",
            "ttl_seconds": 3600,
            "timeout": 5,
        }
    )
    return backend


async def test_send_delivers_on_2xx(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()
    fake_pywebpush["handler"] = lambda **kw: _FakeResponse(201)
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.DELIVERED
    assert "201" in result.message
    # Sanity-check what we passed to pywebpush.
    call = fake_pywebpush["calls"][0]
    assert call["subscription_info"]["endpoint"].startswith("https://fcm.")
    assert call["vapid_claims"] == {"sub": "mailto:gilbert@example.test"}
    assert call["ttl"] == 3600
    assert call["timeout"] == 5
    # Payload is JSON-encoded bytes with the expected SW-facing shape.
    import json as _json

    payload = _json.loads(call["data"].decode("utf-8"))
    assert payload["title"] == "Gilbert"
    assert payload["body"] == "hello"
    assert payload["icon"] == "/icons/gilbert-192.png"
    assert payload["badge"] == "/icons/gilbert-192.png"
    assert payload["tag"] == "n_1"
    assert payload["data"]["url"] == "/"
    assert payload["data"]["notification_id"] == "n_1"
    assert payload["data"]["source"] == "agent"


async def test_send_passes_deep_link_through_to_payload(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()
    fake_pywebpush["handler"] = lambda **kw: _FakeResponse(201)
    msg = _make_message(deep_link="/conversations/c_1")
    await backend.send(_make_destination(), msg)
    import json as _json

    payload = _json.loads(fake_pywebpush["calls"][0]["data"].decode("utf-8"))
    assert payload["data"]["url"] == "/conversations/c_1"


async def test_send_on_410_is_rejected(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()

    def handler(**_: Any) -> Any:
        raise _FakeWebPushException("Gone", _FakeResponse(410))

    fake_pywebpush["handler"] = handler
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.REJECTED
    assert "410" in result.message


async def test_send_on_404_is_rejected(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()

    def handler(**_: Any) -> Any:
        raise _FakeWebPushException("Not found", _FakeResponse(404))

    fake_pywebpush["handler"] = handler
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.REJECTED
    assert "404" in result.message


async def test_send_on_5xx_is_transient_error(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()

    def handler(**_: Any) -> Any:
        raise _FakeWebPushException("Server boom", _FakeResponse(503))

    fake_pywebpush["handler"] = handler
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.TRANSIENT_ERROR
    assert "503" in result.message


async def test_send_on_429_is_transient_with_retry_after(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()

    def handler(**_: Any) -> Any:
        raise _FakeWebPushException(
            "Rate limited",
            _FakeResponse(429, headers={"Retry-After": "12"}),
        )

    fake_pywebpush["handler"] = handler
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.TRANSIENT_ERROR
    assert result.retry_after_s == 12.0


async def test_send_on_429_retry_after_capped_at_60s(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()

    def handler(**_: Any) -> Any:
        raise _FakeWebPushException(
            "Rate limited",
            _FakeResponse(429, headers={"Retry-After": "9999"}),
        )

    fake_pywebpush["handler"] = handler
    result = await backend.send(_make_destination(), _make_message())
    assert result.retry_after_s == 60.0


async def test_send_on_network_error_is_transient(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()

    class _FakeConnError(Exception):
        pass

    def handler(**_: Any) -> Any:
        raise _FakeConnError("connection refused")

    fake_pywebpush["handler"] = handler
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.TRANSIENT_ERROR
    assert "_FakeConnError" in result.message


async def test_send_on_other_4xx_is_rejected(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()

    def handler(**_: Any) -> Any:
        raise _FakeWebPushException("Bad request", _FakeResponse(400))

    fake_pywebpush["handler"] = handler
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.REJECTED
    assert "400" in result.message


# ── Credential scrubbing ─────────────────────────────────────────────


async def test_error_message_scrubs_pem_private_key(
    fake_pywebpush: dict[str, Any],
) -> None:
    """If an error's str() embeds the PEM private key, ``_safe_repr``
    must redact it before it lands in ``PushDeliveryResult.message``."""
    backend = await _initialised_backend()

    def handler(**_: Any) -> Any:
        # Exception with NO response — this routes through
        # ``_classify_webpush_exception``'s no-status-code branch which
        # does feed the raw exception text through ``_safe_repr``.
        raise _FakeWebPushException(
            f"crypto blew up while signing with {_FAKE_PRIVATE_PEM}",
            None,
        )

    fake_pywebpush["handler"] = handler
    result = await backend.send(_make_destination(), _make_message())
    assert "leakthis/secret" not in result.message
    assert "<redacted>" in result.message


async def test_safe_repr_redacts_bearer_token() -> None:
    # Sanity-check the scrubber against the same shape the other push
    # backends test against.
    msg = _safe_repr(RuntimeError("auth: Bearer abc123XYZ secret"))
    assert "abc123XYZ" not in msg
    assert "<redacted>" in msg


async def test_safe_repr_redacts_pem_private_key() -> None:
    msg = _safe_repr(RuntimeError(f"prefix {_FAKE_PRIVATE_PEM} suffix"))
    assert "leakthis/secret" not in msg
    assert "<redacted>" in msg


# ── runtime_data ──────────────────────────────────────────────────────


async def test_runtime_data_exposes_public_key_only() -> None:
    backend = await _initialised_backend()
    data = backend.runtime_data()
    assert data["vapid_public_key"] == _FAKE_PUBLIC
    assert data["has_keys"] is True
    # Private key must NEVER appear in runtime_data.
    assert "vapid_private_key" not in data
    for value in data.values():
        assert _FAKE_PRIVATE_PEM not in str(value)


async def test_runtime_data_signals_unconfigured() -> None:
    backend = WebPush()
    await backend.initialize({})
    data = backend.runtime_data()
    assert data["vapid_public_key"] == ""
    assert data["has_keys"] is False


# ── close() ───────────────────────────────────────────────────────────


async def test_close_disables_subsequent_sends(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()
    await backend.close()
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.DISABLED
    assert fake_pywebpush["calls"] == []


# ── invoke_backend_action — unknown ──────────────────────────────────


async def test_unknown_action_returns_error() -> None:
    backend = WebPush()
    result = await backend.invoke_backend_action("nope", {})
    assert result.status == "error"
    assert "Unknown action" in result.message


async def test_test_connection_action_requires_payload_fields() -> None:
    backend = WebPush()
    await backend.initialize(
        {
            "vapid_public_key": _FAKE_PUBLIC,
            "vapid_private_key": _FAKE_PRIVATE_PEM,
        }
    )
    result = await backend.invoke_backend_action("test_connection", {})
    assert result.status == "error"
    assert (
        "endpoint" in result.message
        and "p256dh" in result.message
        and "auth" in result.message
    )


async def test_test_connection_action_happy_path(
    fake_pywebpush: dict[str, Any],
) -> None:
    backend = await _initialised_backend()
    fake_pywebpush["handler"] = lambda **kw: _FakeResponse(201)
    result = await backend.invoke_backend_action(
        "test_connection",
        {
            "endpoint": "https://fcm.example/test",
            "p256dh": "PUBKEYBLOB",
            "auth": _FAKE_AUTH_SECRET,
        },
    )
    assert result.status == "ok"
    assert fake_pywebpush["calls"][0]["subscription_info"][
        "endpoint"
    ] == "https://fcm.example/test"
