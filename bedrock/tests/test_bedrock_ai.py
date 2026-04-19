"""Tests for AWS Bedrock backend — message/tool conversion + response parsing.

We don't exercise the real Bedrock runtime; the tests mock boto3's client
so the focus is on request-shape and response-shape correctness."""

import base64
from unittest.mock import MagicMock, patch

import pytest
from gilbert_plugin_bedrock.bedrock_ai import BedrockAI

from gilbert.interfaces.ai import (
    AIRequest,
    FileAttachment,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolResult,
)


@pytest.fixture
def backend() -> BedrockAI:
    return BedrockAI()


# --- Initialization ---


async def test_initialize_empty_region_falls_back_to_default(
    backend: BedrockAI,
) -> None:
    """Unlike other AI backends, Bedrock's ``aws_region`` has a
    reasonable default — an empty value falls back to ``us-east-1``
    rather than erroring out."""
    with patch("gilbert_plugin_bedrock.bedrock_ai.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock()
        await backend.initialize({"aws_region": ""})
        _, kwargs = mock_boto3.client.call_args
        assert kwargs["region_name"] == "us-east-1"


async def test_initialize_creates_client(backend: BedrockAI) -> None:
    """boto3.client() is called lazily via asyncio.to_thread — we mock it
    so the test doesn't hit the real AWS SDK credential chain."""
    with patch("gilbert_plugin_bedrock.bedrock_ai.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock()
        await backend.initialize({"aws_region": "us-east-1"})
        assert backend._client is not None
        mock_boto3.client.assert_called_once()
        args, kwargs = mock_boto3.client.call_args
        assert args[0] == "bedrock-runtime"
        assert kwargs["region_name"] == "us-east-1"


async def test_initialize_passes_explicit_credentials(backend: BedrockAI) -> None:
    with patch("gilbert_plugin_bedrock.bedrock_ai.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock()
        await backend.initialize(
            {
                "aws_region": "us-west-2",
                "aws_access_key_id": "AKIA-test",
                "aws_secret_access_key": "secret",
                "aws_session_token": "token",
            }
        )
        _, kwargs = mock_boto3.client.call_args
        assert kwargs["aws_access_key_id"] == "AKIA-test"
        assert kwargs["aws_secret_access_key"] == "secret"
        assert kwargs["aws_session_token"] == "token"


async def test_initialize_omits_credentials_when_blank(backend: BedrockAI) -> None:
    """Blank credential fields should pass ``None`` to boto3 so the
    default credential chain kicks in."""
    with patch("gilbert_plugin_bedrock.bedrock_ai.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock()
        await backend.initialize({"aws_region": "us-east-1"})
        _, kwargs = mock_boto3.client.call_args
        assert kwargs["aws_access_key_id"] is None
        assert kwargs["aws_secret_access_key"] is None
        assert kwargs["aws_session_token"] is None


async def test_close_clears_client(backend: BedrockAI) -> None:
    with patch("gilbert_plugin_bedrock.bedrock_ai.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock()
        await backend.initialize({"aws_region": "us-east-1"})
    await backend.close()
    assert backend._client is None


# --- Capabilities ---


def test_capabilities_reports_streaming_and_attachments() -> None:
    caps = BedrockAI().capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


def test_backend_name() -> None:
    assert BedrockAI.backend_name == "bedrock"


def test_backend_config_params_includes_enabled_toggle() -> None:
    params = BedrockAI.backend_config_params()
    enabled = next((p for p in params if p.key == "enabled"), None)
    assert enabled is not None
    assert enabled.type == ToolParameterType.BOOLEAN
    assert enabled.default is True


def test_model_param_is_free_text() -> None:
    """Bedrock's catalog varies per region — the default model field is
    free-text so users can paste any model ID from the console."""
    params = BedrockAI.backend_config_params()
    model_param = next(p for p in params if p.key == "model")
    assert model_param.choices is None


# --- Message / request conversion ---


def test_build_messages_user_plain_text() -> None:
    backend = BedrockAI()
    result = backend._build_messages(
        [Message(role=MessageRole.USER, content="Hello")]
    )
    assert result == [{"role": "user", "content": [{"text": "Hello"}]}]


def test_build_messages_user_image_attachment_decoded_to_bytes() -> None:
    """Bedrock expects raw image bytes in the ``source.bytes`` field,
    not a base64 string — our builder must decode."""
    backend = BedrockAI()
    raw = b"\x89PNG\r\n\x1a\nfake"
    b64 = base64.b64encode(raw).decode()
    result = backend._build_messages(
        [
            Message(
                role=MessageRole.USER,
                content="describe",
                attachments=[
                    FileAttachment(kind="image", media_type="image/png", data=b64),
                ],
            )
        ]
    )
    blocks = result[0]["content"]
    assert blocks[0] == {
        "image": {"format": "png", "source": {"bytes": raw}}
    }
    assert blocks[1] == {"text": "describe"}


def test_build_messages_assistant_with_tool_calls() -> None:
    backend = BedrockAI()
    result = backend._build_messages(
        [
            Message(
                role=MessageRole.ASSISTANT,
                content="Let me check.",
                tool_calls=[
                    ToolCall(
                        tool_call_id="tu_1",
                        tool_name="search",
                        arguments={"q": "weather"},
                    )
                ],
            )
        ]
    )
    assert result[0]["role"] == "assistant"
    blocks = result[0]["content"]
    assert blocks[0] == {"text": "Let me check."}
    assert blocks[1] == {
        "toolUse": {
            "toolUseId": "tu_1",
            "name": "search",
            # Converse wants the arguments as a JSON-compatible dict, not a string.
            "input": {"q": "weather"},
        }
    }


def test_build_messages_tool_result_becomes_user_role_with_toolresult() -> None:
    backend = BedrockAI()
    result = backend._build_messages(
        [
            Message(
                role=MessageRole.TOOL_RESULT,
                tool_results=[
                    ToolResult(tool_call_id="tu_1", content="found it"),
                    ToolResult(tool_call_id="tu_2", content="failed", is_error=True),
                ],
            )
        ]
    )
    # Bedrock's turn-taking puts tool results inside a user-role message.
    assert result[0]["role"] == "user"
    assert result[0]["content"][0]["toolResult"]["toolUseId"] == "tu_1"
    assert result[0]["content"][0]["toolResult"]["status"] == "success"
    assert result[0]["content"][1]["toolResult"]["status"] == "error"


def test_build_messages_splits_slash_command_combined_row() -> None:
    """Slash-command assistant rows that carry both tool_calls and
    tool_results must be split into an assistant message (tool_use
    blocks) plus a user message (toolResult blocks)."""
    backend = BedrockAI()
    result = backend._build_messages(
        [
            Message(
                role=MessageRole.ASSISTANT,
                content="Here's your recap...",
                tool_calls=[
                    ToolCall(
                        tool_call_id="slash-abc",
                        tool_name="time_logs_recap",
                        arguments={"days": 7},
                    )
                ],
                tool_results=[
                    ToolResult(tool_call_id="slash-abc", content="Recap: 7 days..."),
                ],
            )
        ]
    )
    assert len(result) == 2
    assert result[0]["role"] == "assistant"
    assert any("toolUse" in b for b in result[0]["content"])
    assert result[1]["role"] == "user"
    assert result[1]["content"][0]["toolResult"]["toolUseId"] == "slash-abc"


def test_build_converse_kwargs_wires_system_prompt_and_tools() -> None:
    backend = BedrockAI()
    backend._model = "us.amazon.nova-pro-v1:0"
    backend._max_tokens = 256
    backend._temperature = 0.2
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="hi")],
        system_prompt="Be concise",
        tools=[
            ToolDefinition(
                name="search",
                description="search the web",
                parameters=[
                    ToolParameter(
                        name="q",
                        type=ToolParameterType.STRING,
                        description="query",
                    ),
                ],
            )
        ],
    )
    kwargs = backend._build_converse_kwargs(request)
    assert kwargs["modelId"] == "us.amazon.nova-pro-v1:0"
    assert kwargs["inferenceConfig"] == {"maxTokens": 256, "temperature": 0.2}
    assert kwargs["system"] == [{"text": "Be concise"}]
    assert kwargs["toolConfig"]["tools"][0]["toolSpec"]["name"] == "search"
    assert (
        kwargs["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"][
            "properties"
        ]["q"]["type"]
        == "string"
    )


# --- Response parsing ---


def test_parse_converse_response_text_only() -> None:
    backend = BedrockAI()
    backend._model = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    response = backend._parse_converse_response(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": "Hello!"}],
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
    )
    assert response.message.content == "Hello!"
    assert response.stop_reason == StopReason.END_TURN
    assert response.usage is not None
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5


def test_parse_converse_response_tool_use() -> None:
    backend = BedrockAI()
    response = backend._parse_converse_response(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"text": "Checking..."},
                        {
                            "toolUse": {
                                "toolUseId": "tu_1",
                                "name": "search",
                                "input": {"q": "weather"},
                            }
                        },
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 20, "outputTokens": 15},
        }
    )
    assert response.stop_reason == StopReason.TOOL_USE
    assert response.message.content == "Checking..."
    assert len(response.message.tool_calls) == 1
    tc = response.message.tool_calls[0]
    assert tc.tool_call_id == "tu_1"
    assert tc.tool_name == "search"
    assert tc.arguments == {"q": "weather"}


def test_parse_converse_response_max_tokens() -> None:
    backend = BedrockAI()
    response = backend._parse_converse_response(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": "truncated"}],
                }
            },
            "stopReason": "max_tokens",
        }
    )
    assert response.stop_reason == StopReason.MAX_TOKENS


# --- Error translation ---


def test_error_from_client_error_uses_status_and_message() -> None:
    from botocore.exceptions import ClientError

    exc = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "Not authorized to call InvokeModel",
            },
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "Converse",
    )
    err = BedrockAI._error_from_client_error(exc)
    assert err.status == 403
    assert "403" in str(err)
    assert "AccessDeniedException" in str(err)
    assert "Not authorized" in str(err)


# --- generate (with mocked boto3 client) ---


async def test_generate_calls_converse_in_thread(backend: BedrockAI) -> None:
    from botocore.config import Config as BotoConfig  # noqa: F401

    with patch("gilbert_plugin_bedrock.bedrock_ai.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": "API response"}],
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
        mock_boto3.client.return_value = mock_client
        await backend.initialize({"aws_region": "us-east-1"})

        request = AIRequest(
            messages=[Message(role=MessageRole.USER, content="Test")],
            system_prompt="Be helpful",
        )
        response = await backend.generate(request)

        assert response.message.content == "API response"
        mock_client.converse.assert_called_once()
        kwargs = mock_client.converse.call_args.kwargs
        assert kwargs["system"] == [{"text": "Be helpful"}]
        assert kwargs["messages"][0]["role"] == "user"


async def test_generate_raises_when_not_initialized(backend: BedrockAI) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(
            AIRequest(messages=[Message(role=MessageRole.USER, content="Test")])
        )
