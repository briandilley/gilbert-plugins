"""Tests for Ollama backend — native /api/chat, num_ctx, optional api_key."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from gilbert_plugin_ollama.ollama_ai import OllamaAI

from gilbert.interfaces.ai import (
    AIBackendError,
    AIRequest,
    Message,
    MessageRole,
    StopReason,
    StreamEventType,
)
from gilbert.interfaces.tools import ToolCall, ToolParameterType, ToolResult


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


async def test_default_chat_url_is_native_api(backend: OllamaAI) -> None:
    await backend.initialize({})
    assert backend._chat_url() == "http://localhost:11434/api/chat"
    await backend.close()


async def test_default_model_is_llama3_3(backend: OllamaAI) -> None:
    await backend.initialize({})
    assert backend._model == "llama3.3"
    await backend.close()


async def test_custom_base_url_for_remote_ollama(backend: OllamaAI) -> None:
    await backend.initialize({"base_url": "http://gpu.lan:11434"})
    assert backend._chat_url() == "http://gpu.lan:11434/api/chat"
    await backend.close()


async def test_base_url_v1_suffix_is_stripped(backend: OllamaAI) -> None:
    """A ``base_url`` carried over from the old OpenAI-shim default (ending in
    ``/v1``) is normalised to the daemon root — native routes live there."""
    await backend.initialize({"base_url": "http://gpu.lan:11434/v1"})
    assert backend._root_url() == "http://gpu.lan:11434"
    assert backend._chat_url() == "http://gpu.lan:11434/api/chat"
    assert backend._tags_url() == "http://gpu.lan:11434/api/tags"
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


def test_build_request_body_uses_num_predict_for_max_tokens() -> None:
    backend = OllamaAI()
    backend._model = "llama3.3"
    backend._max_tokens = 256
    body = backend._build_request_body(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    )
    assert body["model"] == "llama3.3"
    # Native API: max_tokens -> options.num_predict, no top-level max_tokens.
    assert "max_tokens" not in body
    assert body["options"]["num_predict"] == 256


def test_build_request_body_sets_num_ctx_from_context_window() -> None:
    """The whole point of the native endpoint: a resolved ``context_window``
    becomes ``options.num_ctx`` so the model loads with the right window."""
    backend = OllamaAI()
    backend._model = "llama3.3"
    body = backend._build_request_body(
        AIRequest(
            messages=[Message(role=MessageRole.USER, content="hi")],
            context_window=32768,
        )
    )
    assert body["options"]["num_ctx"] == 32768


def test_build_request_body_omits_num_ctx_when_unset() -> None:
    """No ``context_window`` -> no ``num_ctx``, so Ollama keeps its own default."""
    backend = OllamaAI()
    body = backend._build_request_body(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    )
    assert "num_ctx" not in body["options"]


def test_parse_text_response() -> None:
    backend = OllamaAI()
    data = {
        "message": {"role": "assistant", "content": "Hello!"},
        "done": True,
        "done_reason": "stop",
        "model": "llama3.3",
        "prompt_eval_count": 9,
        "eval_count": 2,
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.stop_reason == StopReason.END_TURN
    assert response.usage is not None
    assert response.usage.input_tokens == 9
    assert response.usage.output_tokens == 2


def test_parse_response_with_tool_calls() -> None:
    """Native tool calls carry an arguments *object* (not a JSON string) and no
    id — we synthesise an id and report ``TOOL_USE``."""
    backend = OllamaAI()
    data = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": {"city": "SF"}}}
            ],
        },
        "done": True,
        "done_reason": "stop",
        "model": "llama3.3",
    }
    response = backend._parse_response(data)
    assert response.stop_reason == StopReason.TOOL_USE
    assert len(response.message.tool_calls) == 1
    tc = response.message.tool_calls[0]
    assert tc.tool_name == "get_weather"
    assert tc.arguments == {"city": "SF"}
    assert tc.tool_call_id  # synthesised, non-empty


async def test_generate_calls_native_chat_endpoint(backend: OllamaAI) -> None:
    await backend.initialize({})
    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "message": {"role": "assistant", "content": "hi"},
        "done": True,
        "done_reason": "stop",
        "model": "llama3.3",
    }
    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
    await backend.generate(
        AIRequest(messages=[Message(role=MessageRole.USER, content="x")])
    )
    # Posts to the absolute native /api/chat URL with stream disabled.
    assert backend._client.post.call_args[0][0] == "http://localhost:11434/api/chat"
    assert backend._client.post.call_args.kwargs["json"]["stream"] is False
    await backend.close()


async def test_generate_raises_ai_backend_error(backend: OllamaAI) -> None:
    await backend.initialize({})
    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 404
    # Native errors are a plain {"error": "<string>"}.
    mock_response.json.return_value = {"error": "model not found"}
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


# --- Dynamic available_models() from /api/tags + overlay enrichment ---


def test_no_enabled_models_config_param() -> None:
    """The legacy ``enabled_models`` param is gone — per-model ``enabled``
    is enforced by core AIService now (ADR-0019)."""
    params = OllamaAI.backend_config_params()
    assert all(p.key != "enabled_models" for p in params)


def test_available_models_empty_before_refresh() -> None:
    """Dynamic listing: nothing installed (cache empty) → no models."""
    backend = OllamaAI()
    assert backend.available_models() == []


def test_available_models_reflects_installed_tags() -> None:
    """A bare installed tag with no overlay match → ``id == name == tag``."""
    backend = OllamaAI()
    backend._installed_tags = ["some-obscure-model:7b"]
    models = backend.available_models()
    assert len(models) == 1
    assert models[0].id == "some-obscure-model:7b"
    assert models[0].name == "some-obscure-model:7b"


def test_available_models_enriches_from_overlay_exact() -> None:
    """An installed tag matching the recommended overlay borrows its
    friendly name/description; the advertised id stays the real tag."""
    backend = OllamaAI()
    backend._installed_tags = ["llama3.3"]
    models = backend.available_models()
    assert len(models) == 1
    assert models[0].id == "llama3.3"
    assert models[0].name == "Llama 3.3 70B"
    assert "Meta" in models[0].description


def test_available_models_enriches_from_overlay_by_family() -> None:
    """``qwen2.5-coder:32b`` borrows the ``qwen2.5-coder`` overlay name but
    advertises the full installed tag as its id so chat round-trips it."""
    backend = OllamaAI()
    backend._installed_tags = ["qwen2.5-coder:32b"]
    models = backend.available_models()
    assert len(models) == 1
    assert models[0].id == "qwen2.5-coder:32b"
    assert models[0].name == "Qwen 2.5 Coder"


async def test_refresh_installed_models_queries_api_tags(backend: OllamaAI) -> None:
    """``refresh_installed_models`` parses ``/api/tags`` into the cache and
    queries the daemon root (not the ``/v1`` chat path)."""
    await backend.initialize({})  # initialize seeds via refresh — mock after
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "models": [
            {"name": "llama3.3:latest", "size": 42_000_000_000},
            {"name": "mistral:7b"},
        ]
    }
    assert backend._client is not None
    backend._client.get = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
    await backend.refresh_installed_models()
    assert backend._installed_tags == ["llama3.3:latest", "mistral:7b"]
    # Hit the native /api/tags route at the daemon root.
    called_url = backend._client.get.call_args[0][0]
    assert called_url.endswith("/api/tags")
    assert "/v1/" not in called_url
    await backend.close()


async def test_refresh_installed_models_survives_daemon_down(
    backend: OllamaAI,
) -> None:
    """A daemon that's unreachable leaves the cache untouched, not crashing."""
    await backend.initialize({})
    backend._installed_tags = ["llama3.3"]
    assert backend._client is not None
    backend._client.get = AsyncMock(side_effect=httpx.ConnectError("down"))  # type: ignore[method-assign]
    await backend.refresh_installed_models()
    assert backend._installed_tags == ["llama3.3"]
    await backend.close()


# --- Per-model generation param consumption ---


def test_request_temperature_and_max_tokens_override_globals() -> None:
    """Resolved per-call values on the request override the backend globals."""
    backend = OllamaAI()
    backend._model = "llama3.3"
    backend._max_tokens = 8192
    backend._temperature = 0.7
    body = backend._build_request_body(
        AIRequest(
            messages=[Message(role=MessageRole.USER, content="hi")],
            temperature=0.2,
            max_tokens=256,
        )
    )
    assert body["options"]["temperature"] == 0.2
    assert body["options"]["num_predict"] == 256


def test_request_falls_back_to_globals_when_none() -> None:
    """``None`` on the request means 'use the backend default'."""
    backend = OllamaAI()
    backend._model = "llama3.3"
    backend._max_tokens = 8192
    backend._temperature = 0.7
    body = backend._build_request_body(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    )
    assert body["options"]["temperature"] == 0.7
    assert body["options"]["num_predict"] == 8192


# --- Native message encoding (tools, tool results, images) ---


def test_build_messages_assistant_tool_call_uses_object_arguments() -> None:
    """Native assistant tool_calls carry an arguments *object* and no id."""
    backend = OllamaAI()
    rows = backend._build_messages(
        [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(tool_call_id="call_0", tool_name="ping", arguments={"n": 1})
                ],
            )
        ],
        system_prompt="",
    )
    tc = rows[0]["tool_calls"][0]
    assert tc == {"function": {"name": "ping", "arguments": {"n": 1}}}
    assert "id" not in tc


def test_build_messages_tool_result_is_native_tool_row() -> None:
    """A tool result becomes ``{"role": "tool", content, tool_name}`` — the
    name resolved from the matching assistant tool_call (no id on the wire)."""
    backend = OllamaAI()
    rows = backend._build_messages(
        [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(tool_call_id="call_0", tool_name="ping", arguments={})
                ],
            ),
            Message(
                role=MessageRole.TOOL_RESULT,
                tool_results=[ToolResult(tool_call_id="call_0", content="pong")],
            ),
        ],
        system_prompt="",
    )
    tool_row = rows[-1]
    assert tool_row["role"] == "tool"
    assert tool_row["content"] == "pong"
    assert tool_row["tool_name"] == "ping"
    assert "tool_call_id" not in tool_row


def test_build_user_message_inlines_images_as_base64_list() -> None:
    """Image attachments with inline bytes go in a native ``images`` array as
    raw base64 (no ``data:`` URL), with the text kept in ``content``."""
    from gilbert.interfaces.ai import FileAttachment

    backend = OllamaAI()
    row = backend._build_user_message(
        Message(
            role=MessageRole.USER,
            content="what is this?",
            attachments=[
                FileAttachment(
                    kind="image",
                    name="pic.png",
                    media_type="image/png",
                    size=3,
                    data="QUJD",
                )
            ],
        )
    )
    assert row["images"] == ["QUJD"]
    assert row["content"] == "what is this?"


# --- Native NDJSON streaming ---


def _streaming_backend(lines: list[dict], capture: dict) -> OllamaAI:
    """An OllamaAI whose client streams ``lines`` as native NDJSON."""
    body = "".join(json.dumps(line) + "\n" for line in lines)

    def handler(request: httpx.Request) -> httpx.Response:
        capture["path"] = request.url.path
        capture["body"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    backend = OllamaAI()
    backend._base_url = "http://localhost:11434"
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return backend


async def test_generate_stream_parses_native_ndjson() -> None:
    capture: dict = {}
    backend = _streaming_backend(
        [
            {"model": "llama3.3", "message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"model": "llama3.3", "message": {"role": "assistant", "content": "lo"}, "done": False},
            {
                "model": "llama3.3",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 12,
                "eval_count": 3,
            },
        ],
        capture,
    )
    events = [
        ev
        async for ev in backend.generate_stream(
            AIRequest(
                messages=[Message(role=MessageRole.USER, content="hi")],
                context_window=16384,
            )
        )
    ]
    await backend.close()

    # Hit the native endpoint, streaming, with num_ctx applied.
    assert capture["path"] == "/api/chat"
    assert capture["body"]["stream"] is True
    assert capture["body"]["options"]["num_ctx"] == 16384

    text = "".join(e.text or "" for e in events if e.type == StreamEventType.TEXT_DELTA)
    assert text == "Hello"
    complete = next(e for e in events if e.type == StreamEventType.MESSAGE_COMPLETE)
    assert complete.response is not None
    assert complete.response.message.content == "Hello"
    assert complete.response.stop_reason == StopReason.END_TURN
    assert complete.response.usage.input_tokens == 12
    assert complete.response.usage.output_tokens == 3


async def test_ensure_model_info_parses_show() -> None:
    """``/api/show`` populates the model's context_length + thinking support."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/show"
        return httpx.Response(
            200,
            json={
                "capabilities": ["completion", "thinking"],
                "model_info": {"qwen3.context_length": 262144},
            },
        )

    backend = OllamaAI()
    backend._base_url = "http://localhost:11434"
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    info = await backend._ensure_model_info("qwen3:8b")
    await backend.close()
    assert info["context_length"] == 262144
    assert info["thinking"] is True


def test_build_request_body_clamps_num_ctx_to_model_max() -> None:
    """A context window beyond the model's max is clamped — an impossible
    num_ctx makes Ollama hang allocating a KV cache it can't fit."""
    backend = OllamaAI()
    backend._model = "m"
    backend._model_info["m"] = {"context_length": 32768, "thinking": False}
    body = backend._build_request_body(
        AIRequest(
            messages=[Message(role=MessageRole.USER, content="hi")],
            context_window=999999,
        )
    )
    assert body["options"]["num_ctx"] == 32768
    assert "think" not in body


def test_build_request_body_sets_think_when_model_supports_it() -> None:
    backend = OllamaAI()
    backend._model = "m"
    backend._model_info["m"] = {"context_length": None, "thinking": True}
    body = backend._build_request_body(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    )
    assert body["think"] is True


async def test_generate_stream_emits_reasoning_deltas() -> None:
    """``message.thinking`` deltas surface as REASONING_DELTA and accumulate
    onto the final response's ``message.reasoning`` (distinct from content)."""
    capture: dict = {}
    backend = _streaming_backend(
        [
            {"model": "m", "message": {"role": "assistant", "thinking": "Let me "}, "done": False},
            {"model": "m", "message": {"role": "assistant", "thinking": "think."}, "done": False},
            {
                "model": "m",
                "message": {"role": "assistant", "content": "Answer"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 3,
                "eval_count": 2,
            },
        ],
        capture,
    )
    backend._model_info["m"] = {"context_length": None, "thinking": True}
    events = [
        ev
        async for ev in backend.generate_stream(
            AIRequest(model="m", messages=[Message(role=MessageRole.USER, content="hi")])
        )
    ]
    await backend.close()

    assert capture["body"]["think"] is True
    reasoning = "".join(
        e.text or "" for e in events if e.type == StreamEventType.REASONING_DELTA
    )
    assert reasoning == "Let me think."
    complete = next(e for e in events if e.type == StreamEventType.MESSAGE_COMPLETE)
    assert complete.response.message.reasoning == "Let me think."
    assert complete.response.message.content == "Answer"


async def test_generate_raises_clear_error_on_timeout() -> None:
    """An httpx timeout becomes a descriptive AIBackendError (504), not a bare
    exception that surfaces as 'Unknown error'."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    backend = OllamaAI()
    backend._base_url = "http://localhost:11434"
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend._model_info["m"] = {"context_length": None, "thinking": False}
    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(
            AIRequest(
                model="m",
                messages=[Message(role=MessageRole.USER, content="hi")],
                context_window=999999,
            )
        )
    await backend.close()
    assert exc_info.value.status == 504
    assert "timed out" in str(exc_info.value).lower()


def test_request_timeout_config_param_exists() -> None:
    """The request timeout must be operator-configurable. A large local
    reasoning model (cold 18GB load + long thinking) legitimately needs more
    than the old hardcoded 120s, and only the operator knows their hardware."""
    params = OllamaAI.backend_config_params()
    rt = next((p for p in params if p.key == "request_timeout"), None)
    assert rt is not None
    assert rt.type == ToolParameterType.INTEGER


async def test_request_timeout_is_applied_to_client_read_timeout() -> None:
    """A configured ``request_timeout`` governs how long Gilbert waits for the
    model to respond (httpx *read* timeout), so a slow local model isn't killed
    mid-generation or mid-load."""
    backend = OllamaAI()
    await backend.initialize({"request_timeout": 600})
    assert backend._client is not None
    assert backend._client.timeout.read == 600.0
    await backend.close()


async def test_default_request_timeout_exceeds_two_minutes() -> None:
    """The old hardcoded 120s killed a cold-loading 18GB CPU model before it
    emitted its first token. The default must give local models real headroom."""
    backend = OllamaAI()
    await backend.initialize({})
    assert backend._client is not None
    assert backend._client.timeout.read is not None
    assert backend._client.timeout.read > 120.0
    await backend.close()


async def test_request_timeout_zero_disables_read_timeout() -> None:
    """``0`` means 'wait as long as the daemon needs' — for very slow CPU-only
    inference where any finite cap risks truncating a valid response."""
    backend = OllamaAI()
    await backend.initialize({"request_timeout": 0})
    assert backend._client is not None
    assert backend._client.timeout.read is None
    await backend.close()


async def test_connect_timeout_stays_short_for_fast_failure() -> None:
    """Raising the generation timeout must not also make an unreachable daemon
    hang: the *connect* timeout stays short so a down daemon fails fast."""
    backend = OllamaAI()
    await backend.initialize({"request_timeout": 600})
    assert backend._client is not None
    assert backend._client.timeout.connect is not None
    assert backend._client.timeout.connect <= 30.0
    await backend.close()


async def test_generate_stream_emits_tool_calls() -> None:
    capture: dict = {}
    backend = _streaming_backend(
        [
            {
                "model": "m",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "get_weather", "arguments": {"city": "SF"}}}
                    ],
                },
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 5,
                "eval_count": 2,
            }
        ],
        capture,
    )
    events = [
        ev
        async for ev in backend.generate_stream(
            AIRequest(messages=[Message(role=MessageRole.USER, content="weather?")])
        )
    ]
    await backend.close()

    starts = [e for e in events if e.type == StreamEventType.TOOL_CALL_START]
    assert len(starts) == 1 and starts[0].tool_name == "get_weather"
    complete = next(e for e in events if e.type == StreamEventType.MESSAGE_COMPLETE)
    assert complete.response.stop_reason == StopReason.TOOL_USE
    tc = complete.response.message.tool_calls[0]
    assert tc.tool_name == "get_weather"
    assert tc.arguments == {"city": "SF"}
