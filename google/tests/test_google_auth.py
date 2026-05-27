"""Tests for GoogleAuthBackend callback-URL source selection."""

import logging
from types import SimpleNamespace

from gilbert_plugin_google.google_auth import GoogleAuthBackend

_CB = "/auth/login/google/callback"


def _tunnel(url: str = "https://abc.ngrok.io") -> SimpleNamespace:
    return SimpleNamespace(
        public_url=url,
        public_url_for=lambda p: f"{url}{p}",
    )


def _internal(url: str = "https://192-168-1-50.sslip.io:8443") -> SimpleNamespace:
    return SimpleNamespace(
        internal_url=url,
        internal_url_for=lambda p: f"{url}{p}",
    )


async def test_config_params_include_callback_source() -> None:
    params = GoogleAuthBackend.backend_config_params()
    src = next(p for p in params if p.key == "callback_url_source")
    assert src.choices == ("tunnel", "internal_url", "request")
    assert src.default == "tunnel"


async def test_tunnel_source_uses_tunnel_url() -> None:
    be = GoogleAuthBackend()
    await be.initialize({"client_id": "x", "callback_url_source": "tunnel"})
    be.set_tunnel(_tunnel())
    be.set_internal_url(_internal())
    assert be.get_callback_url("https://req.local") == f"https://abc.ngrok.io{_CB}"


async def test_internal_url_source_uses_internal_url() -> None:
    be = GoogleAuthBackend()
    await be.initialize({"client_id": "x", "callback_url_source": "internal_url"})
    be.set_tunnel(_tunnel())
    be.set_internal_url(_internal())
    assert (
        be.get_callback_url("https://req.local")
        == f"https://192-168-1-50.sslip.io:8443{_CB}"
    )


async def test_request_source_uses_request_origin() -> None:
    be = GoogleAuthBackend()
    await be.initialize({"client_id": "x", "callback_url_source": "request"})
    be.set_tunnel(_tunnel())
    be.set_internal_url(_internal())
    assert be.get_callback_url("https://req.local/") == f"https://req.local{_CB}"


async def test_falls_back_to_request_when_chosen_source_missing(
    caplog: logging.LogRecord,
) -> None:
    be = GoogleAuthBackend()
    await be.initialize({"client_id": "x", "callback_url_source": "internal_url"})
    # internal_url never injected (service disabled) → fall back to origin.
    with caplog.at_level(logging.WARNING):
        url = be.get_callback_url("https://req.local")
    assert url == f"https://req.local{_CB}"
    assert any("internal_url" in r.message for r in caplog.records)


async def test_fallback_warning_logged_once(caplog: logging.LogRecord) -> None:
    be = GoogleAuthBackend()
    await be.initialize({"client_id": "x", "callback_url_source": "tunnel"})
    with caplog.at_level(logging.WARNING):
        be.get_callback_url("https://req.local")
        be.get_callback_url("https://req.local")
    warnings = [r for r in caplog.records if "callback_url_source" in r.message]
    assert len(warnings) == 1


async def test_default_source_is_tunnel() -> None:
    be = GoogleAuthBackend()
    await be.initialize({"client_id": "x"})
    be.set_tunnel(_tunnel())
    assert be.get_callback_url("https://req.local") == f"https://abc.ngrok.io{_CB}"
