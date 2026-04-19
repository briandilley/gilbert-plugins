"""Tests for OpenAIAI backend — message translation, response parsing, streaming."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_openai.openai_ai import OpenAIAI

from gilbert.interfaces.ai import (
    AIBackendError,
    AIRequest,
    FileAttachment,
    Message,
    MessageRole,
    StopReason,
    StreamEventType,
)
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolResult,
)


@pytest.fixture
def backend() -> OpenAIAI:
    return OpenAIAI()


# --- Initialization ---


async def test_initialize_requires_api_key(backend: OpenAIAI) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_initialize_creates_client(backend: OpenAIAI) -> None:
    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None
    await backend.close()


async def test_initialize_custom_model(backend: OpenAIAI) -> None:
    await backend.initialize({"api_key": "sk-test", "model": "gpt-4o-mini"})
    assert backend._model == "gpt-4o-mini"
    await backend.close()


async def test_initialize_sets_organization_header(backend: OpenAIAI) -> None:
    await backend.initialize({"api_key": "sk-test", "organization": "org-abc"})
    assert backend._client is not None
    assert backend._client.headers.get("OpenAI-Organization") == "org-abc"
    await backend.close()


async def test_initialize_custom_base_url(backend: OpenAIAI) -> None:
    """A user-supplied base URL with a trailing slash must join cleanly
    with the ``/chat/completions`` path — no double slash."""
    await backend.initialize(
        {"api_key": "sk-test", "base_url": "https://proxy.example/v1/"}
    )
    assert backend._client is not None
    # Resolve a relative endpoint the way httpx would for a real request
    # and check we don't end up with ``/v1//chat/completions``.
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == "https://proxy.example/v1/chat/completions"
    await backend.close()


async def test_close_clears_client(backend: OpenAIAI) -> None:
    await backend.initialize({"api_key": "sk-test"})
    await backend.close()
    assert backend._client is None


# --- Capabilities ---


def test_capabilities_reports_streaming_and_attachments() -> None:
    caps = OpenAIAI().capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


def test_available_models_filtered_by_enabled() -> None:
    backend = OpenAIAI()
    backend._enabled_models = ["gpt-4o-mini"]
    models = backend.available_models()
    assert [m.id for m in models] == ["gpt-4o-mini"]


# --- Request building ---


def test_build_messages_user_plain_text() -> None:
    backend = OpenAIAI()
    messages = [Message(role=MessageRole.USER, content="Hello")]
    result = backend._build_messages(messages, "")
    assert result == [{"role": "user", "content": "Hello"}]


def test_build_messages_system_prompt_prepended() -> None:
    backend = OpenAIAI()
    messages = [Message(role=MessageRole.USER, content="Hi")]
    result = backend._build_messages(messages, "Be helpful")
    assert result[0] == {"role": "system", "content": "Be helpful"}
    assert result[1] == {"role": "user", "content": "Hi"}


def test_build_messages_historical_system_row_kept() -> None:
    backend = OpenAIAI()
    messages = [
        Message(role=MessageRole.SYSTEM, content="extra system"),
        Message(role=MessageRole.USER, content="hi"),
    ]
    result = backend._build_messages(messages, "Be helpful")
    # Both the request-level system prompt AND the historical system
    # row show up; we can't drop the historical one without losing
    # information, so they're both emitted as ``role=system``.
    assert result[0] == {"role": "system", "content": "Be helpful"}
    assert result[1] == {"role": "system", "content": "extra system"}
    assert result[2] == {"role": "user", "content": "hi"}


def test_build_messages_user_with_image_attachment() -> None:
    backend = OpenAIAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="what is this?",
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
    assert content[1] == {"type": "text", "text": "what is this?"}


def test_build_messages_user_image_without_text() -> None:
    backend = OpenAIAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="",
            attachments=[
                FileAttachment(kind="image", media_type="image/jpeg", data="BBBB"),
            ],
        )
    ]
    result = backend._build_messages(messages, "")
    content = result[0]["content"]
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0]["type"] == "image_url"


def test_build_messages_user_with_document_renders_as_text_stub() -> None:
    """OpenAI Chat Completions can't ingest PDFs natively, so document
    attachments become a text stub pointing at the workspace tools."""
    backend = OpenAIAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="summarize",
            attachments=[
                FileAttachment(
                    kind="document",
                    name="report.pdf",
                    media_type="application/pdf",
                    data="PDFBYTES",
                    size=1024,
                ),
            ],
        )
    ]
    result = backend._build_messages(messages, "")
    content = result[0]["content"]
    assert content[0]["type"] == "text"
    assert "report.pdf" in content[0]["text"]
    assert "application/pdf" in content[0]["text"]
    assert "read_workspace_file" in content[0]["text"]
    assert content[1] == {"type": "text", "text": "summarize"}


def test_build_messages_user_with_text_attachment() -> None:
    backend = OpenAIAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="explain",
            attachments=[
                FileAttachment(
                    kind="text",
                    name="notes.md",
                    media_type="text/markdown",
                    text="# hello world",
                ),
            ],
        )
    ]
    result = backend._build_messages(messages, "")
    content = result[0]["content"]
    assert content[0] == {"type": "text", "text": "## notes.md\n\n# hello world"}
    assert content[1] == {"type": "text", "text": "explain"}


def test_build_messages_mixed_attachment_ordering() -> None:
    """Order is image → document stub → text → file stub → user prompt,
    regardless of the order the attachments were declared in."""
    backend = OpenAIAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="compare",
            attachments=[
                FileAttachment(
                    kind="file",
                    name="binary.dat",
                    media_type="application/octet-stream",
                    data="AAAA",
                    size=16,
                ),
                FileAttachment(
                    kind="text",
                    name="notes.md",
                    media_type="text/markdown",
                    text="text body",
                ),
                FileAttachment(
                    kind="document",
                    name="r.pdf",
                    media_type="application/pdf",
                    data="PDF",
                    size=8,
                ),
                FileAttachment(kind="image", media_type="image/png", data="IMG"),
            ],
        )
    ]
    result = backend._build_messages(messages, "")
    content = result[0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[1]["type"] == "text"
    assert "r.pdf" in content[1]["text"]
    assert content[2] == {"type": "text", "text": "## notes.md\n\ntext body"}
    assert content[3]["type"] == "text"
    assert "binary.dat" in content[3]["text"]
    assert content[4] == {"type": "text", "text": "compare"}


def test_build_messages_assistant_text_only() -> None:
    backend = OpenAIAI()
    messages = [Message(role=MessageRole.ASSISTANT, content="Hi there")]
    result = backend._build_messages(messages, "")
    assert result == [{"role": "assistant", "content": "Hi there"}]


def test_build_messages_assistant_with_tool_calls() -> None:
    backend = OpenAIAI()
    messages = [
        Message(
            role=MessageRole.ASSISTANT,
            content="Let me check.",
            tool_calls=[
                ToolCall(
                    tool_call_id="tc_1",
                    tool_name="search",
                    arguments={"q": "test"},
                )
            ],
        )
    ]
    result = backend._build_messages(messages, "")
    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "Let me check."
    tcs = result[0]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "tc_1"
    assert tcs[0]["type"] == "function"
    assert tcs[0]["function"]["name"] == "search"
    # Arguments must be JSON-encoded as a string, not a dict.
    assert tcs[0]["function"]["arguments"] == '{"q": "test"}'


def test_build_messages_tool_result() -> None:
    backend = OpenAIAI()
    messages = [
        Message(
            role=MessageRole.TOOL_RESULT,
            tool_results=[
                ToolResult(tool_call_id="tc_1", content="found it"),
                ToolResult(tool_call_id="tc_2", content="failed", is_error=True),
            ],
        )
    ]
    result = backend._build_messages(messages, "")
    # Each tool result becomes its own ``role=tool`` row.
    assert result == [
        {"role": "tool", "tool_call_id": "tc_1", "content": "found it"},
        {"role": "tool", "tool_call_id": "tc_2", "content": "failed"},
    ]


def test_build_messages_splits_slash_command_combined_row() -> None:
    """Assistant rows carrying both tool_calls and tool_results must be
    split into an assistant(tool_calls) + N tool-role rows, with the
    assistant text preserved as the tool_calls row's content."""
    backend = OpenAIAI()
    messages = [
        Message(role=MessageRole.USER, content="/recap 7d"),
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
                ToolResult(
                    tool_call_id="slash-abc",
                    content="Recap: 7 days...",
                    is_error=False,
                )
            ],
        ),
        Message(role=MessageRole.USER, content="show me more"),
    ]
    result = backend._build_messages(messages, "")
    assert len(result) == 4
    assert result[0] == {"role": "user", "content": "/recap 7d"}
    assert result[1]["role"] == "assistant"
    assert result[1]["content"] == "Here's your recap..."
    assert len(result[1]["tool_calls"]) == 1
    assert result[2] == {
        "role": "tool",
        "tool_call_id": "slash-abc",
        "content": "Recap: 7 days...",
    }
    assert result[3] == {"role": "user", "content": "show me more"}


def test_build_tools() -> None:
    tools = [
        ToolDefinition(
            name="search",
            description="Search for things",
            parameters=[
                ToolParameter(
                    name="query",
                    type=ToolParameterType.STRING,
                    description="Search query",
                ),
            ],
        ),
    ]
    result = OpenAIAI._build_tools(tools)
    assert len(result) == 1
    assert result[0]["type"] == "function"
    fn = result[0]["function"]
    assert fn["name"] == "search"
    assert fn["description"] == "Search for things"
    assert fn["parameters"]["properties"]["query"]["type"] == "string"


def test_build_request_body_uses_max_completion_tokens() -> None:
    backend = OpenAIAI()
    backend._model = "gpt-4o"
    backend._max_tokens = 123
    backend._temperature = 0.4
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
        system_prompt="Be helpful",
    )
    body = backend._build_request_body(request)
    assert body["model"] == "gpt-4o"
    assert body["max_completion_tokens"] == 123
    assert body["temperature"] == 0.4
    assert "max_tokens" not in body
    # System prompt rides as the first message, not a top-level field.
    assert body["messages"][0] == {"role": "system", "content": "Be helpful"}


def test_build_request_body_omits_temperature_for_reasoning_models() -> None:
    """``o``-series reasoning models only accept the default sampling;
    sending a temperature causes a 400, so the request builder must
    omit it entirely."""
    backend = OpenAIAI()
    backend._model = "o1-mini"
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
    )
    body = backend._build_request_body(request)
    assert "temperature" not in body


def test_build_request_body_omits_empty_tools() -> None:
    backend = OpenAIAI()
    backend._model = "gpt-4o"
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
    )
    body = backend._build_request_body(request)
    assert "tools" not in body


def test_build_request_body_honours_per_request_model() -> None:
    backend = OpenAIAI()
    backend._model = "gpt-4o"
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
        model="gpt-4o-mini",
    )
    body = backend._build_request_body(request)
    assert body["model"] == "gpt-4o-mini"


# --- Response parsing ---


def test_parse_text_response() -> None:
    backend = OpenAIAI()
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.message.role == MessageRole.ASSISTANT
    assert response.model == "gpt-4o"
    assert response.stop_reason == StopReason.END_TURN
    assert response.usage is not None
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5


def test_parse_tool_use_response() -> None:
    backend = OpenAIAI()
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Checking...",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": '{"q": "weather"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 20, "completion_tokens": 15},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Checking..."
    assert len(response.message.tool_calls) == 1
    assert response.message.tool_calls[0].tool_call_id == "call_123"
    assert response.message.tool_calls[0].tool_name == "search"
    assert response.message.tool_calls[0].arguments == {"q": "weather"}
    assert response.stop_reason == StopReason.TOOL_USE


def test_parse_max_tokens_response() -> None:
    backend = OpenAIAI()
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Truncated..."},
                "finish_reason": "length",
            }
        ],
        "model": "gpt-4o",
    }
    response = backend._parse_response(data)
    assert response.stop_reason == StopReason.MAX_TOKENS


def test_parse_no_usage() -> None:
    backend = OpenAIAI()
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }
        ],
        "model": "gpt-4o",
    }
    response = backend._parse_response(data)
    assert response.usage is None


def test_parse_null_content_with_tool_calls() -> None:
    """OpenAI frequently returns ``"content": null`` alongside tool_calls.
    Treat that as empty text, not as an error."""
    backend = OpenAIAI()
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "x", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "model": "gpt-4o",
    }
    response = backend._parse_response(data)
    assert response.message.content == ""
    assert len(response.message.tool_calls) == 1


# --- generate (integration with mocked HTTP) ---


async def test_generate_calls_api(backend: OpenAIAI) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "API response"},
                "finish_reason": "stop",
            }
        ],
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Test")],
        system_prompt="Be helpful",
    )
    response = await backend.generate(request)

    assert response.message.content == "API response"
    backend._client.post.assert_called_once()
    call_args = backend._client.post.call_args
    assert call_args[0][0] == "/chat/completions"

    await backend.close()


async def test_generate_raises_ai_backend_error_on_http_error(
    backend: OpenAIAI,
) -> None:
    """A 4xx response should surface OpenAI's error.message, not opaque HTTP text."""
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 400
    mock_response.json.return_value = {
        "error": {
            "type": "invalid_request_error",
            "message": "Invalid model: not-a-model",
        }
    }

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    request = AIRequest(messages=[Message(role=MessageRole.USER, content="Test")])

    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(request)

    assert exc_info.value.status == 400
    assert "Invalid model: not-a-model" in str(exc_info.value)
    assert "400" in str(exc_info.value)

    await backend.close()


async def test_generate_raises_ai_backend_error_on_non_json_error(
    backend: OpenAIAI,
) -> None:
    """A 5xx with a non-JSON body should still produce a non-empty error message."""
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 502
    mock_response.json.side_effect = ValueError("not json")
    mock_response.text = "<html>Bad Gateway</html>"

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    request = AIRequest(messages=[Message(role=MessageRole.USER, content="Test")])
    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(request)

    assert exc_info.value.status == 502
    assert "Bad Gateway" in str(exc_info.value)

    await backend.close()


async def test_generate_raises_when_not_initialized(backend: OpenAIAI) -> None:
    request = AIRequest(messages=[Message(role=MessageRole.USER, content="Test")])
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(request)


# --- Streaming (generate_stream via mocked SSE) ---


class _FakeStreamResponse:
    """Minimal async-context-manager stand-in for httpx streaming response.

    Feeds a canned sequence of SSE lines (as ``list[str]``) into
    ``generate_stream`` without a real httpx transport. Blank-line event
    delimiters are part of the input so the parser sees the wire shape.
    """

    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code
        self.is_error = status_code >= 400
        self._body_bytes = b""

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def aiter_lines(self):  # type: ignore[no-untyped-def]
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body_bytes


def _sse_chunk(payload: dict) -> list[str]:
    import json as _j

    return [f"data: {_j.dumps(payload)}", ""]


async def test_generate_stream_text_deltas_and_complete(backend: OpenAIAI) -> None:
    """OpenAI streams text via ``delta.content`` chunks; the final
    chunk carries ``finish_reason=stop`` and the usage payload."""
    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None

    lines: list[str] = []
    lines += _sse_chunk(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Hello "},
                    "finish_reason": None,
                }
            ],
        }
    )
    lines += _sse_chunk(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "world"},
                    "finish_reason": None,
                }
            ],
        }
    )
    lines += _sse_chunk(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
    )
    lines.append("data: [DONE]")
    lines.append("")

    fake = _FakeStreamResponse(lines)
    backend._client.stream = MagicMock(return_value=fake)  # type: ignore[method-assign]

    events = []
    async for ev in backend.generate_stream(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    ):
        events.append(ev)

    text_events = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
    assert [e.text for e in text_events] == ["Hello ", "world"]

    completes = [e for e in events if e.type == StreamEventType.MESSAGE_COMPLETE]
    assert len(completes) == 1
    final = completes[0].response
    assert final is not None
    assert final.message.content == "Hello world"
    assert final.stop_reason == StopReason.END_TURN
    assert final.usage is not None
    assert final.usage.input_tokens == 12
    assert final.usage.output_tokens == 3

    await backend.close()


async def test_generate_stream_tool_call_reassembly(backend: OpenAIAI) -> None:
    """The first tool_call delta carries ``id`` + ``function.name``;
    subsequent deltas carry only ``function.arguments`` chunks. The
    streamer must assemble them into a complete ``ToolCall``."""
    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None

    lines: list[str] = []
    lines += _sse_chunk(
        {
            "id": "chatcmpl-2",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": None},
                    "finish_reason": None,
                }
            ],
        }
    )
    lines += _sse_chunk(
        {
            "id": "chatcmpl-2",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "",
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
    )
    lines += _sse_chunk(
        {
            "id": "chatcmpl-2",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '{"city": "Portl'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
    )
    lines += _sse_chunk(
        {
            "id": "chatcmpl-2",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": 'and"}'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
    )
    lines += _sse_chunk(
        {
            "id": "chatcmpl-2",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8},
        }
    )
    lines.append("data: [DONE]")
    lines.append("")

    fake = _FakeStreamResponse(lines)
    backend._client.stream = MagicMock(return_value=fake)  # type: ignore[method-assign]

    events = []
    async for ev in backend.generate_stream(
        AIRequest(messages=[Message(role=MessageRole.USER, content="weather?")])
    ):
        events.append(ev)

    starts = [e for e in events if e.type == StreamEventType.TOOL_CALL_START]
    deltas = [e for e in events if e.type == StreamEventType.TOOL_CALL_DELTA]
    ends = [e for e in events if e.type == StreamEventType.TOOL_CALL_END]
    assert len(starts) == 1
    assert starts[0].tool_call_id == "call_abc"
    assert starts[0].tool_name == "get_weather"
    assert len(deltas) == 2
    assert len(ends) == 1

    completes = [e for e in events if e.type == StreamEventType.MESSAGE_COMPLETE]
    assert len(completes) == 1
    final = completes[0].response
    assert final is not None
    assert final.stop_reason == StopReason.TOOL_USE
    assert len(final.message.tool_calls) == 1
    tc = final.message.tool_calls[0]
    assert tc.tool_call_id == "call_abc"
    assert tc.tool_name == "get_weather"
    assert tc.arguments == {"city": "Portland"}

    await backend.close()


async def test_generate_stream_max_tokens_stop_reason(backend: OpenAIAI) -> None:
    """A ``finish_reason="length"`` SSE event maps to ``StopReason.MAX_TOKENS``."""
    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None

    lines: list[str] = []
    lines += _sse_chunk(
        {
            "id": "chatcmpl-3",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "truncated"},
                    "finish_reason": None,
                }
            ],
        }
    )
    lines += _sse_chunk(
        {
            "id": "chatcmpl-3",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4096},
        }
    )
    lines.append("data: [DONE]")
    lines.append("")

    fake = _FakeStreamResponse(lines)
    backend._client.stream = MagicMock(return_value=fake)  # type: ignore[method-assign]

    final_response = None
    async for ev in backend.generate_stream(
        AIRequest(messages=[Message(role=MessageRole.USER, content="long answer")])
    ):
        if ev.type == StreamEventType.MESSAGE_COMPLETE:
            final_response = ev.response

    assert final_response is not None
    assert final_response.stop_reason == StopReason.MAX_TOKENS
    assert final_response.message.content == "truncated"

    await backend.close()


async def test_generate_stream_raises_on_http_error(backend: OpenAIAI) -> None:
    """A streaming endpoint error body should be surfaced as AIBackendError."""
    import json as _j

    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None

    class _ErrStream(_FakeStreamResponse):
        def __init__(self) -> None:
            super().__init__(lines=[], status_code=401)
            self._body_bytes = _j.dumps(
                {"error": {"message": "Invalid API key"}}
            ).encode()

    backend._client.stream = MagicMock(return_value=_ErrStream())  # type: ignore[method-assign]

    with pytest.raises(AIBackendError) as exc_info:
        async for _ev in backend.generate_stream(
            AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
        ):
            pass

    assert exc_info.value.status == 401
    assert "Invalid API key" in str(exc_info.value)

    await backend.close()
