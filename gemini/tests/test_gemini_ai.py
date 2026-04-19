"""Tests for Gemini backend — vendor defaults + core contract."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_gemini.gemini_ai import GeminiAI

from gilbert.interfaces.ai import (
    AIBackendError,
    AIRequest,
    FileAttachment,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.tools import ToolParameterType


@pytest.fixture
def backend() -> GeminiAI:
    return GeminiAI()


async def test_initialize_requires_api_key(backend: GeminiAI) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_default_base_url_is_google_compat(backend: GeminiAI) -> None:
    await backend.initialize({"api_key": "AIza-test"})
    assert backend._client is not None
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    await backend.close()


async def test_default_model_is_gemini_2_5_flash(backend: GeminiAI) -> None:
    await backend.initialize({"api_key": "AIza-test"})
    assert backend._model == "gemini-2.5-flash"
    await backend.close()


def test_capabilities_reports_streaming_and_attachments() -> None:
    caps = GeminiAI().capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


def test_backend_name() -> None:
    assert GeminiAI.backend_name == "gemini"


def test_backend_config_params_includes_enabled_toggle() -> None:
    params = GeminiAI.backend_config_params()
    enabled = next((p for p in params if p.key == "enabled"), None)
    assert enabled is not None
    assert enabled.type == ToolParameterType.BOOLEAN
    assert enabled.default is True


def test_build_messages_image_uses_image_url_part() -> None:
    """Gemini is natively multimodal — image attachments are sent as
    OpenAI-shape image_url parts on the compat endpoint."""
    backend = GeminiAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="describe",
            attachments=[
                FileAttachment(kind="image", media_type="image/png", data="AAAA"),
            ],
        )
    ]
    result = backend._build_messages(messages, "")
    content = result[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_build_request_body_uses_max_tokens() -> None:
    backend = GeminiAI()
    backend._model = "gemini-2.5-flash"
    backend._max_tokens = 256
    body = backend._build_request_body(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    )
    assert body["model"] == "gemini-2.5-flash"
    assert body["max_tokens"] == 256


def test_parse_text_response() -> None:
    backend = GeminiAI()
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "model": "gemini-2.5-flash",
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.stop_reason == StopReason.END_TURN


async def test_generate_calls_chat_completions(backend: GeminiAI) -> None:
    await backend.initialize({"api_key": "AIza-test"})
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "model": "gemini-2.5-flash",
    }
    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    await backend.generate(
        AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
    )
    assert backend._client.post.call_args[0][0] == "/chat/completions"
    await backend.close()


async def test_generate_raises_ai_backend_error(backend: GeminiAI) -> None:
    await backend.initialize({"api_key": "AIza-test"})
    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 401
    mock_response.json.return_value = {"error": {"message": "Invalid API key"}}
    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(
            AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
        )
    assert exc_info.value.status == 401
    await backend.close()


async def test_generate_raises_when_not_initialized(backend: GeminiAI) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(
            AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
        )
