"""Tests for Ollama backend — local defaults, optional api_key."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_ollama.ollama_ai import OllamaAI

from gilbert.interfaces.ai import (
    AIBackendError,
    AIRequest,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.tools import ToolParameterType


@pytest.fixture
def backend() -> OllamaAI:
    return OllamaAI()


async def test_initialize_without_api_key_succeeds(backend: OllamaAI) -> None:
    """Local Ollama doesn't require auth — the backend must start up
    without an api_key, unlike every other AI backend."""
    await backend.initialize({})
    assert backend._client is not None
    # No Authorization header when no key was provided.
    assert "Authorization" not in backend._client.headers
    await backend.close()


async def test_initialize_with_api_key_sets_bearer(backend: OllamaAI) -> None:
    """If a key is configured (for a proxied Ollama), it flows through
    as a Bearer token."""
    await backend.initialize({"api_key": "proxy-secret"})
    assert backend._client is not None
    assert backend._client.headers.get("Authorization") == "Bearer proxy-secret"
    await backend.close()


async def test_default_base_url_is_localhost_ollama(backend: OllamaAI) -> None:
    await backend.initialize({})
    assert backend._client is not None
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == "http://localhost:11434/v1/chat/completions"
    await backend.close()


async def test_default_model_is_llama3_3(backend: OllamaAI) -> None:
    await backend.initialize({})
    assert backend._model == "llama3.3"
    await backend.close()


async def test_custom_base_url_for_remote_ollama(backend: OllamaAI) -> None:
    await backend.initialize({"base_url": "http://gpu.lan:11434/v1"})
    assert backend._client is not None
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == "http://gpu.lan:11434/v1/chat/completions"
    await backend.close()


async def test_arbitrary_model_tag_accepted(backend: OllamaAI) -> None:
    """The model set is whatever the user has pulled — free-text."""
    await backend.initialize({"model": "qwen2.5-coder:32b"})
    assert backend._model == "qwen2.5-coder:32b"
    await backend.close()


def test_capabilities_reports_streaming_and_attachments() -> None:
    caps = OllamaAI().capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


def test_backend_name() -> None:
    assert OllamaAI.backend_name == "ollama"


def test_api_key_default_is_empty() -> None:
    """Ollama's api_key param defaults to '' and is not required — the
    UI must present it as optional, not gate backend init on it."""
    params = OllamaAI.backend_config_params()
    api_key = next(p for p in params if p.key == "api_key")
    assert api_key.default == ""
    assert api_key.sensitive is True


def test_model_param_has_no_choices() -> None:
    """Ollama's available models = whatever the user pulled. Must be
    free-text."""
    params = OllamaAI.backend_config_params()
    model_param = next(p for p in params if p.key == "model")
    assert model_param.choices is None


def test_backend_config_params_includes_enabled_toggle() -> None:
    params = OllamaAI.backend_config_params()
    enabled = next((p for p in params if p.key == "enabled"), None)
    assert enabled is not None
    assert enabled.type == ToolParameterType.BOOLEAN
    assert enabled.default is True


def test_build_request_body_uses_max_tokens() -> None:
    backend = OllamaAI()
    backend._model = "llama3.3"
    backend._max_tokens = 256
    body = backend._build_request_body(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    )
    assert body["model"] == "llama3.3"
    assert body["max_tokens"] == 256


def test_parse_text_response() -> None:
    backend = OllamaAI()
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "model": "llama3.3",
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.stop_reason == StopReason.END_TURN


async def test_generate_calls_chat_completions(backend: OllamaAI) -> None:
    await backend.initialize({})
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "model": "llama3.3",
    }
    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    await backend.generate(
        AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
    )
    assert backend._client.post.call_args[0][0] == "/chat/completions"
    await backend.close()


async def test_generate_raises_ai_backend_error(backend: OllamaAI) -> None:
    await backend.initialize({})
    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 404
    mock_response.json.return_value = {"error": {"message": "model not found"}}
    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(
            AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
        )
    assert exc_info.value.status == 404
    assert "model not found" in str(exc_info.value)
    await backend.close()


async def test_generate_raises_when_not_initialized(backend: OllamaAI) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(
            AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
        )
