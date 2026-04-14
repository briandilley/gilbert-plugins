"""Unit tests for the Gmail email backend.

These tests focus on the MIME message construction inside ``send()`` —
specifically that Reply-To and a formatted From header are applied
correctly. The Gmail API is stubbed with a minimal fake service that
captures the raw base64-encoded payload so we can assert on the headers.
"""

from __future__ import annotations

import base64
from email import message_from_bytes
from email.message import Message
from typing import Any

import pytest

from gilbert.interfaces.email import EmailAddress
from gilbert_plugin_google.gmail import GmailBackend


class _FakeSendRequest:
    def __init__(self, captured: dict[str, Any], body: dict[str, Any]) -> None:
        self._captured = captured
        self._body = body

    def execute(self) -> dict[str, str]:
        self._captured.update(self._body)
        return {"id": "sent_123"}


class _FakeMessagesResource:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def send(self, userId: str, body: dict[str, Any]) -> _FakeSendRequest:  # noqa: N803
        # Matches the google-api-python-client Gmail send signature.
        return _FakeSendRequest(self._captured, body)


class _FakeUsersResource:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def messages(self) -> _FakeMessagesResource:
        return _FakeMessagesResource(self._captured)


class _FakeGmailService:
    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    def users(self) -> _FakeUsersResource:
        return _FakeUsersResource(self.captured)


def _decode_raw(raw: str) -> Message:
    # Gmail API expects url-safe base64; decode and parse back into a MIME message
    payload = base64.urlsafe_b64decode(raw.encode("ascii"))
    return message_from_bytes(payload)


@pytest.fixture
def backend_with_fake_service() -> tuple[GmailBackend, _FakeGmailService]:
    backend = GmailBackend()
    backend._email_address = "assistant@example.com"
    fake = _FakeGmailService()
    backend._service = fake
    return backend, fake


@pytest.mark.asyncio
async def test_send_default_from_and_no_reply_to(
    backend_with_fake_service: tuple[GmailBackend, _FakeGmailService],
) -> None:
    backend, fake = backend_with_fake_service

    await backend.send(
        to=[EmailAddress(email="customer@example.com")],
        subject="Hello",
        body_html="<p>Hi</p>",
    )

    msg = _decode_raw(fake.captured["raw"])
    assert msg["From"] == "assistant@example.com"
    assert msg["Reply-To"] is None
    assert msg["To"] == "customer@example.com"


@pytest.mark.asyncio
async def test_send_formats_from_name(
    backend_with_fake_service: tuple[GmailBackend, _FakeGmailService],
) -> None:
    backend, fake = backend_with_fake_service

    await backend.send(
        to=[EmailAddress(email="customer@example.com")],
        subject="Hello",
        body_html="<p>Hi</p>",
        from_name="Example Co",
    )

    msg = _decode_raw(fake.captured["raw"])
    assert msg["From"] == "Example Co <assistant@example.com>"


@pytest.mark.asyncio
async def test_send_sets_reply_to(
    backend_with_fake_service: tuple[GmailBackend, _FakeGmailService],
) -> None:
    backend, fake = backend_with_fake_service

    await backend.send(
        to=[EmailAddress(email="customer@example.com")],
        subject="Hello",
        body_html="<p>Hi</p>",
        reply_to=EmailAddress(email="sales@example.com"),
    )

    msg = _decode_raw(fake.captured["raw"])
    assert msg["Reply-To"] == "sales@example.com"


@pytest.mark.asyncio
async def test_send_sets_reply_to_with_name(
    backend_with_fake_service: tuple[GmailBackend, _FakeGmailService],
) -> None:
    backend, fake = backend_with_fake_service

    await backend.send(
        to=[EmailAddress(email="customer@example.com")],
        subject="Hello",
        body_html="<p>Hi</p>",
        reply_to=EmailAddress(email="sales@example.com", name="Example Sales"),
        from_name="Example Co",
    )

    msg = _decode_raw(fake.captured["raw"])
    assert msg["From"] == "Example Co <assistant@example.com>"
    assert msg["Reply-To"] == "Example Sales <sales@example.com>"
