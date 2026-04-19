"""Tests for MistralAI backend — vendor defaults + core contract."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_mistral.mistral_ai import MistralAI

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
def backend() -> MistralAI:
    return MistralAI()


async def test_initialize_requires_api_key(backend: MistralAI) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_default_base_url_is_la_plateforme(backend: MistralAI) -> None:
    await backend.initialize({"api_key": "test"})
    assert backend._client is not None
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == "https://api.mistral.ai/v1/chat/completions"
    await backend.close()


async def test_default_model_is_mistral_large(backend: MistralAI) -> None:
    await backend.initialize({"api_key": "test"})
    assert backend._model == "mistral-large-latest"
    await backend.close()


def test_capabilities_reports_streaming_and_attachments() -> None:
    caps = MistralAI().capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


def test_backend_name() -> None:
    assert MistralAI.backend_name == "mistral"


def test_backend_config_params_includes_enabled_toggle() -> None:
    params = MistralAI.backend_config_params()
    enabled = next((p for p in params if p.key == "enabled"), None)
    assert enabled is not None
    assert enabled.type == ToolParameterType.BOOLEAN
    assert enabled.default is True


def test_build_request_body_uses_max_tokens() -> None:
    backend = MistralAI()
    backend._model = "mistral-large-latest"
    backend._max_tokens = 256
    backend._temperature = 0.3
    body = backend._build_request_body(
        AIRequest(
            messages=[Message(role=MessageRole.USER, content="hi")],
            system_prompt="Be terse",
        )
    )
    assert body["model"] == "mistral-large-latest"
    assert body["max_tokens"] == 256
    assert "max_completion_tokens" not in body
    assert body["temperature"] == 0.3


def test_build_messages_image_uses_image_url_part() -> None:
    """Pixtral models accept image_url parts — attachments with data
    are inlined as OpenAI-shape data URLs rather than text stubs."""
    backend = MistralAI()
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
    assert content[0] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }
    assert content[1] == {"type": "text", "text": "describe"}


def test_parse_text_response() -> None:
    backend = MistralAI()
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "model": "mistral-large-latest",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.stop_reason == StopReason.END_TURN
    assert response.usage is not None


async def test_generate_calls_chat_completions(backend: MistralAI) -> None:
    await backend.initialize({"api_key": "test"})
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "model": "mistral-large-latest",
    }
    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    await backend.generate(
        AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
    )
    assert backend._client.post.call_args[0][0] == "/chat/completions"
    await backend.close()


async def test_generate_raises_ai_backend_error(backend: MistralAI) -> None:
    await backend.initialize({"api_key": "test"})
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


async def test_generate_raises_when_not_initialized(backend: MistralAI) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(
            AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
        )
