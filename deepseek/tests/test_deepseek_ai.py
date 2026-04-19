"""Tests for DeepSeekAI backend — focuses on vendor-specific defaults and
the core request/response contract. Deep OpenAI-compat behaviour (tool-call
reassembly, attachment ordering, slash-command splitting) is covered by
the qwen plugin's tests since the parsing logic is identical."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_deepseek.deepseek_ai import DeepSeekAI

from gilbert.interfaces.ai import (
    AIBackendError,
    AIRequest,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.tools import ToolParameterType


@pytest.fixture
def backend() -> DeepSeekAI:
    return DeepSeekAI()


async def test_initialize_requires_api_key(backend: DeepSeekAI) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_default_base_url_is_deepseek(backend: DeepSeekAI) -> None:
    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == "https://api.deepseek.com/v1/chat/completions"
    await backend.close()


async def test_default_model_is_deepseek_chat(backend: DeepSeekAI) -> None:
    await backend.initialize({"api_key": "sk-test"})
    assert backend._model == "deepseek-chat"
    await backend.close()


async def test_custom_base_url_joins_cleanly(backend: DeepSeekAI) -> None:
    await backend.initialize(
        {"api_key": "sk-test", "base_url": "https://proxy.example/v1/"}
    )
    assert backend._client is not None
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == "https://proxy.example/v1/chat/completions"
    await backend.close()


def test_capabilities_reports_streaming_and_attachments() -> None:
    caps = DeepSeekAI().capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


def test_backend_name() -> None:
    assert DeepSeekAI.backend_name == "deepseek"


def test_backend_config_params_includes_enabled_toggle() -> None:
    params = DeepSeekAI.backend_config_params()
    enabled = next((p for p in params if p.key == "enabled"), None)
    assert enabled is not None
    assert enabled.type == ToolParameterType.BOOLEAN
    assert enabled.default is True


def test_build_request_body_uses_max_tokens() -> None:
    backend = DeepSeekAI()
    backend._model = "deepseek-chat"
    backend._max_tokens = 256
    backend._temperature = 0.3
    body = backend._build_request_body(
        AIRequest(
            messages=[Message(role=MessageRole.USER, content="hi")],
            system_prompt="Be terse",
        )
    )
    assert body["model"] == "deepseek-chat"
    assert body["max_tokens"] == 256
    assert "max_completion_tokens" not in body
    assert body["temperature"] == 0.3
    assert body["messages"][0] == {"role": "system", "content": "Be terse"}


def test_parse_text_response() -> None:
    backend = DeepSeekAI()
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "model": "deepseek-chat",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.stop_reason == StopReason.END_TURN
    assert response.model == "deepseek-chat"
    assert response.usage is not None
    assert response.usage.input_tokens == 10


async def test_generate_calls_chat_completions(backend: DeepSeekAI) -> None:
    await backend.initialize({"api_key": "sk-test"})
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "model": "deepseek-chat",
    }
    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    await backend.generate(AIRequest(messages=[Message(role=MessageRole.USER, content="x")]))
    backend._client.post.assert_called_once()
    assert backend._client.post.call_args[0][0] == "/chat/completions"
    await backend.close()


async def test_generate_raises_ai_backend_error_on_http_error(
    backend: DeepSeekAI,
) -> None:
    await backend.initialize({"api_key": "sk-test"})
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


async def test_generate_raises_when_not_initialized(backend: DeepSeekAI) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(
            AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
        )
