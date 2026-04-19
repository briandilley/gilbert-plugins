"""Tests for OpenRouter backend — vendor defaults + attribution headers."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_openrouter.openrouter_ai import OpenRouterAI

from gilbert.interfaces.ai import (
    AIBackendError,
    AIRequest,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.tools import ToolParameterType


@pytest.fixture
def backend() -> OpenRouterAI:
    return OpenRouterAI()


async def test_initialize_requires_api_key(backend: OpenRouterAI) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_default_base_url_is_openrouter(backend: OpenRouterAI) -> None:
    await backend.initialize({"api_key": "sk-or-v1-test"})
    assert backend._client is not None
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == "https://openrouter.ai/api/v1/chat/completions"
    await backend.close()


async def test_default_model_is_claude_sonnet(backend: OpenRouterAI) -> None:
    await backend.initialize({"api_key": "sk-or-v1-test"})
    assert backend._model == "anthropic/claude-sonnet-4-5"
    await backend.close()


async def test_attribution_headers_applied(backend: OpenRouterAI) -> None:
    """site_url / site_name become HTTP-Referer / X-Title headers so
    OpenRouter can attribute usage on their public leaderboard."""
    await backend.initialize(
        {
            "api_key": "sk-or-v1-test",
            "site_url": "https://example.com",
            "site_name": "Gilbert",
        }
    )
    assert backend._client is not None
    assert backend._client.headers.get("HTTP-Referer") == "https://example.com"
    assert backend._client.headers.get("X-Title") == "Gilbert"
    await backend.close()


async def test_attribution_headers_omitted_when_blank(backend: OpenRouterAI) -> None:
    await backend.initialize({"api_key": "sk-or-v1-test"})
    assert backend._client is not None
    assert "HTTP-Referer" not in backend._client.headers
    assert "X-Title" not in backend._client.headers
    await backend.close()


async def test_model_accepts_arbitrary_slug(backend: OpenRouterAI) -> None:
    """OpenRouter's catalog is too large to enumerate — the model field
    must accept any ``provider/model`` slug the user types."""
    await backend.initialize(
        {"api_key": "sk-or-v1-test", "model": "some/obscure-model-v7"}
    )
    assert backend._model == "some/obscure-model-v7"
    await backend.close()


def test_capabilities_reports_streaming_and_attachments() -> None:
    caps = OpenRouterAI().capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


def test_backend_name() -> None:
    assert OpenRouterAI.backend_name == "openrouter"


def test_backend_config_params_includes_enabled_toggle() -> None:
    params = OpenRouterAI.backend_config_params()
    enabled = next((p for p in params if p.key == "enabled"), None)
    assert enabled is not None
    assert enabled.type == ToolParameterType.BOOLEAN
    assert enabled.default is True


def test_model_param_has_no_choices() -> None:
    """OpenRouter's model catalog is dynamic and unbounded, so the
    model field must be free-text rather than a restricted dropdown."""
    params = OpenRouterAI.backend_config_params()
    model_param = next(p for p in params if p.key == "model")
    assert model_param.choices is None


def test_build_request_body_uses_max_tokens() -> None:
    backend = OpenRouterAI()
    backend._model = "anthropic/claude-sonnet-4-5"
    backend._max_tokens = 256
    backend._temperature = 0.3
    body = backend._build_request_body(
        AIRequest(
            messages=[Message(role=MessageRole.USER, content="hi")],
        )
    )
    assert body["model"] == "anthropic/claude-sonnet-4-5"
    assert body["max_tokens"] == 256


def test_parse_text_response() -> None:
    backend = OpenRouterAI()
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "model": "anthropic/claude-sonnet-4-5",
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.stop_reason == StopReason.END_TURN


async def test_generate_calls_chat_completions(backend: OpenRouterAI) -> None:
    await backend.initialize({"api_key": "sk-or-v1-test"})
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "model": "anthropic/claude-sonnet-4-5",
    }
    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    await backend.generate(
        AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
    )
    assert backend._client.post.call_args[0][0] == "/chat/completions"
    await backend.close()


async def test_generate_raises_ai_backend_error(backend: OpenRouterAI) -> None:
    await backend.initialize({"api_key": "sk-or-v1-test"})
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


async def test_generate_raises_when_not_initialized(backend: OpenRouterAI) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(
            AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
        )
