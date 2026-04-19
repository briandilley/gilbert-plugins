"""Tests for GroqAI backend — vendor-specific defaults + core contract."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_groq.groq_ai import GroqAI

from gilbert.interfaces.ai import (
    AIBackendError,
    AIRequest,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.tools import ToolParameterType


@pytest.fixture
def backend() -> GroqAI:
    return GroqAI()


async def test_initialize_requires_api_key(backend: GroqAI) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_default_base_url_is_groq(backend: GroqAI) -> None:
    await backend.initialize({"api_key": "gsk_test"})
    assert backend._client is not None
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == "https://api.groq.com/openai/v1/chat/completions"
    await backend.close()


async def test_default_model_is_llama_3_3(backend: GroqAI) -> None:
    await backend.initialize({"api_key": "gsk_test"})
    assert backend._model == "llama-3.3-70b-versatile"
    await backend.close()


def test_capabilities_reports_streaming_and_attachments() -> None:
    caps = GroqAI().capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


def test_backend_name() -> None:
    assert GroqAI.backend_name == "groq"


def test_backend_config_params_includes_enabled_toggle() -> None:
    params = GroqAI.backend_config_params()
    enabled = next((p for p in params if p.key == "enabled"), None)
    assert enabled is not None
    assert enabled.type == ToolParameterType.BOOLEAN
    assert enabled.default is True


def test_build_request_body_uses_max_tokens() -> None:
    backend = GroqAI()
    backend._model = "llama-3.3-70b-versatile"
    backend._max_tokens = 256
    backend._temperature = 0.3
    body = backend._build_request_body(
        AIRequest(
            messages=[Message(role=MessageRole.USER, content="hi")],
            system_prompt="Be terse",
        )
    )
    assert body["model"] == "llama-3.3-70b-versatile"
    assert body["max_tokens"] == 256
    assert "max_completion_tokens" not in body
    assert body["temperature"] == 0.3


def test_parse_text_response() -> None:
    backend = GroqAI()
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "model": "llama-3.3-70b-versatile",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.stop_reason == StopReason.END_TURN
    assert response.usage is not None
    assert response.usage.input_tokens == 10


async def test_generate_calls_chat_completions(backend: GroqAI) -> None:
    await backend.initialize({"api_key": "gsk_test"})
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "model": "llama-3.3-70b-versatile",
    }
    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    await backend.generate(
        AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
    )
    backend._client.post.assert_called_once()
    assert backend._client.post.call_args[0][0] == "/chat/completions"
    await backend.close()


async def test_generate_raises_ai_backend_error_on_http_error(
    backend: GroqAI,
) -> None:
    await backend.initialize({"api_key": "gsk_test"})
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
    assert "Invalid API key" in str(exc_info.value)
    await backend.close()


async def test_generate_raises_when_not_initialized(backend: GroqAI) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(
            AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
        )
