"""OpenAI GPT AI backend — AI via the OpenAI Chat Completions API."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gilbert.interfaces.ai import (
    AIBackend,
    AIBackendCapabilities,
    AIBackendError,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    ModelInfo,
    StopReason,
    StreamEvent,
    StreamEventType,
    TokenUsage,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameterType,
)

logger = logging.getLogger(__name__)
ai_logger = logging.getLogger("gilbert.ai")

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o"

_AVAILABLE_MODELS = [
    ModelInfo(
        id="gpt-4o",
        name="GPT-4o",
        description="Flagship multimodal model — text, image, and tool use.",
    ),
    ModelInfo(
        id="gpt-4o-mini",
        name="GPT-4o mini",
        description="Small and fast — ideal for simple tasks and high-volume use.",
    ),
    ModelInfo(
        id="gpt-4-turbo",
        name="GPT-4 Turbo",
        description="Previous-generation flagship with vision support.",
    ),
    ModelInfo(
        id="o1",
        name="o1",
        description="Reasoning model — stronger for math, code, and planning.",
    ),
    ModelInfo(
        id="o1-mini",
        name="o1 mini",
        description="Smaller reasoning model — cheaper and faster than o1.",
    ),
    ModelInfo(
        id="o3-mini",
        name="o3 mini",
        description="Next-generation small reasoning model.",
    ),
]


def _is_reasoning_model(model_id: str) -> bool:
    """OpenAI's ``o``-series models (o1, o3, ...) don't accept arbitrary
    ``temperature`` values and use ``max_completion_tokens`` rather than
    the legacy ``max_tokens`` field. Detect them by prefix."""
    return model_id.startswith(("o1", "o3", "o4"))


class OpenAIAI(AIBackend):
    """AI backend using the OpenAI Chat Completions API via httpx."""

    backend_name = "openai"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        all_model_ids = [m.id for m in _AVAILABLE_MODELS]
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Initialize this backend at startup. Uncheck to hide its "
                    "settings and skip initialization."
                ),
                default=True,
            ),
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="OpenAI API key (``sk-…``).",
                sensitive=True,
            ),
            ConfigParam(
                key="base_url",
                type=ToolParameterType.STRING,
                description=(
                    "API base URL. Leave at the default for OpenAI; override "
                    "to point at an OpenAI-compatible endpoint (Azure OpenAI, "
                    "a local proxy, etc.)."
                ),
                default=_DEFAULT_BASE_URL,
            ),
            ConfigParam(
                key="organization",
                type=ToolParameterType.STRING,
                description=(
                    "Optional OpenAI organization ID — sent as the "
                    "``OpenAI-Organization`` header. Leave blank unless your "
                    "account belongs to multiple orgs."
                ),
                default="",
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description=(
                    "Default model ID used when no per-request model is specified."
                ),
                default=_DEFAULT_MODEL,
                choices=tuple(all_model_ids),
            ),
            ConfigParam(
                key="enabled_models",
                type=ToolParameterType.ARRAY,
                description=(
                    "Models available for selection in the chat UI and model "
                    "tier mappings. Only enabled models can be assigned to tiers."
                ),
                default=all_model_ids,
                choices=tuple(all_model_ids),
            ),
            ConfigParam(
                key="max_tokens",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum tokens in a single AI response. Sent as "
                    "``max_completion_tokens`` to the API so it works for both "
                    "classic chat models and the ``o``-series reasoning models."
                ),
                default=16384,
            ),
            ConfigParam(
                key="temperature",
                type=ToolParameterType.NUMBER,
                description=(
                    "Sampling temperature (0.0 = deterministic, 1.0 = creative). "
                    "Ignored for ``o``-series reasoning models, which only "
                    "accept the default sampling."
                ),
                default=0.7,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Send a tiny 'hi' message to the OpenAI API to verify the API key and model."
                ),
            ),
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict,
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(
                status="error",
                message="OpenAI backend is not initialized — save settings first.",
            )
        try:
            request = AIRequest(
                messages=[Message(role=MessageRole.USER, content="hi")],
                system_prompt="Reply with a single word.",
                tools=[],
            )
            response = await self.generate(request)
        except AIBackendError as exc:
            return ConfigActionResult(
                status="error",
                message=f"OpenAI API error: {exc}",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=f"Connected to OpenAI (model: {response.model}).",
        )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._model: str = _DEFAULT_MODEL
        self._enabled_models: list[str] = [m.id for m in _AVAILABLE_MODELS]
        self._max_tokens: int = 16384
        self._temperature: float = 0.7

    async def initialize(self, config: dict[str, Any]) -> None:
        api_key = config.get("api_key")
        if not api_key:
            raise ValueError("OpenAIAI requires 'api_key' in config")

        self._model = str(config.get("model", _DEFAULT_MODEL))
        raw_enabled = config.get("enabled_models")
        if isinstance(raw_enabled, list) and raw_enabled:
            self._enabled_models = [str(m) for m in raw_enabled]
        self._max_tokens = int(config.get("max_tokens", 16384))
        self._temperature = float(config.get("temperature", 0.7))
        base_url = str(config.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        org = str(config.get("organization") or "").strip()
        if org:
            headers["OpenAI-Organization"] = org

        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=120.0,
        )
        logger.info("OpenAI AI backend initialized (model=%s)", self._model)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def available_models(self) -> list[ModelInfo]:
        return [m for m in _AVAILABLE_MODELS if m.id in self._enabled_models]

    def capabilities(self) -> AIBackendCapabilities:
        return AIBackendCapabilities(
            streaming=True,
            attachments_user=True,
        )

    async def generate(self, request: AIRequest) -> AIResponse:
        if self._client is None:
            raise RuntimeError("OpenAIAI not initialized")

        body = self._build_request_body(request)

        ai_logger.debug(
            "OpenAI request: model=%s messages=%d",
            body["model"],
            len(body["messages"]),
        )

        resp = await self._client.post("/chat/completions", json=body)
        if resp.is_error:
            raise self._error_from_response(resp.status_code, resp, body)
        data = resp.json()

        ai_logger.debug(
            "OpenAI response: finish_reason=%s usage=%s",
            self._first_finish_reason(data),
            data.get("usage"),
        )

        return self._parse_response(data)

    async def generate_stream(
        self,
        request: AIRequest,
    ) -> AsyncIterator[StreamEvent]:
        """Stream OpenAI SSE chunks as provider-neutral ``StreamEvent``s.

        Hits ``POST /v1/chat/completions`` with ``stream: true`` and
        ``stream_options.include_usage: true`` so the final chunk carries
        the token counts. Parses the SSE frames into:

        - ``delta.content`` → ``TEXT_DELTA``
        - ``delta.tool_calls[i]`` (first appearance with ``id`` + ``name``)
          → ``TOOL_CALL_START``
        - ``delta.tool_calls[i].function.arguments`` chunks → ``TOOL_CALL_DELTA``
        - tool call visible with a ``finish_reason="tool_calls"`` → ``TOOL_CALL_END``
        - final chunk → ``MESSAGE_COMPLETE`` with the assembled ``AIResponse``

        All OpenAI-specific SSE parsing lives in this method; the core
        ``AIService`` agentic loop only sees the neutral events.
        """
        if self._client is None:
            raise RuntimeError("OpenAIAI not initialized")

        body = self._build_request_body(request)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

        ai_logger.debug(
            "OpenAI stream request: model=%s messages=%d",
            self._model,
            len(body["messages"]),
        )

        text_parts: list[str] = []
        # Accumulators keyed by the tool_call index the API uses to
        # associate deltas. OpenAI sends the id + name on the first delta
        # for a given index and streams the arguments JSON incrementally
        # after that.
        tool_builders: dict[int, dict[str, Any]] = {}
        tool_started: set[int] = set()
        tool_ended: set[int] = set()
        finish_reason_raw = "stop"
        usage_input = 0
        usage_output = 0
        model_id = self._model

        async with self._client.stream(
            "POST",
            "/chat/completions",
            json=body,
        ) as resp:
            if resp.is_error:
                err_bytes = await resp.aread()
                raise self._error_from_stream_body(
                    resp.status_code,
                    err_bytes,
                )

            async for line in resp.aiter_lines():
                # SSE: the only lines we care about are ``data: ...``;
                # blank lines and ``: keepalive`` comments get skipped.
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue

                model_id = str(data.get("model") or model_id)
                usage = data.get("usage")
                if isinstance(usage, dict):
                    usage_input = int(usage.get("prompt_tokens", usage_input) or 0)
                    usage_output = int(
                        usage.get("completion_tokens", usage_output) or 0
                    )

                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue

                delta = choice.get("delta") or {}
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        text_parts.append(content)
                        yield StreamEvent(
                            type=StreamEventType.TEXT_DELTA,
                            text=content,
                        )
                    delta_tool_calls = delta.get("tool_calls")
                    if isinstance(delta_tool_calls, list):
                        for tc_delta in delta_tool_calls:
                            if not isinstance(tc_delta, dict):
                                continue
                            async for ev in self._ingest_tool_call_delta(
                                tc_delta,
                                tool_builders,
                                tool_started,
                            ):
                                yield ev

                raw_finish = choice.get("finish_reason")
                if raw_finish:
                    finish_reason_raw = str(raw_finish)
                    if finish_reason_raw == "tool_calls":
                        for idx in sorted(tool_builders.keys()):
                            if idx in tool_ended:
                                continue
                            builder = tool_builders[idx]
                            tool_ended.add(idx)
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_END,
                                tool_call_id=str(builder.get("id", "")),
                                tool_name=str(builder.get("name", "")),
                            )

        stop_reason = self._map_finish_reason(finish_reason_raw)

        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_builders.keys()):
            builder = tool_builders[idx]
            raw_json = builder.get("arguments", "")
            try:
                args = json.loads(raw_json) if raw_json else {}
            except json.JSONDecodeError:
                # Streaming was cut off mid-JSON — leave args empty and
                # let the core loop's max_tokens handler surface the
                # truncation error to the user.
                args = {}
            tool_calls.append(
                ToolCall(
                    tool_call_id=str(builder.get("id", "")),
                    tool_name=str(builder.get("name", "")),
                    arguments=args,
                )
            )

        final_message = Message(
            role=MessageRole.ASSISTANT,
            content="".join(text_parts),
            tool_calls=tool_calls,
        )
        usage_obj = TokenUsage(
            input_tokens=usage_input,
            output_tokens=usage_output,
        )
        final_response = AIResponse(
            message=final_message,
            model=model_id,
            stop_reason=stop_reason,
            usage=usage_obj,
        )
        ai_logger.debug(
            "OpenAI stream response: finish_reason=%s usage=%s",
            finish_reason_raw,
            {"prompt_tokens": usage_input, "completion_tokens": usage_output},
        )
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            response=final_response,
        )

    async def _ingest_tool_call_delta(
        self,
        tc_delta: dict[str, Any],
        tool_builders: dict[int, dict[str, Any]],
        tool_started: set[int],
    ) -> AsyncIterator[StreamEvent]:
        """Fold a single streamed ``tool_calls[i]`` delta into the builder.

        The first delta for a given index carries the tool's ``id`` and
        ``function.name``; subsequent deltas only carry partial
        ``function.arguments`` strings. We emit ``TOOL_CALL_START`` the
        first time we see a complete name for an index, and
        ``TOOL_CALL_DELTA`` for every non-empty argument chunk.
        """
        idx = int(tc_delta.get("index") or 0)
        builder = tool_builders.setdefault(
            idx,
            {"id": "", "name": "", "arguments": ""},
        )

        tc_id = tc_delta.get("id")
        if isinstance(tc_id, str) and tc_id:
            builder["id"] = tc_id

        fn = tc_delta.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and name:
                builder["name"] = name
            args_chunk = fn.get("arguments")
            if isinstance(args_chunk, str) and args_chunk:
                builder["arguments"] += args_chunk
                if idx in tool_started:
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_DELTA,
                        tool_call_id=str(builder.get("id", "")),
                        tool_name=str(builder.get("name", "")),
                        partial_json=args_chunk,
                    )

        if idx not in tool_started and builder.get("id") and builder.get("name"):
            tool_started.add(idx)
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_START,
                tool_call_id=str(builder["id"]),
                tool_name=str(builder["name"]),
            )
            # If arguments already accumulated before the start fired
            # (name arrived on a later delta than the first args chunk),
            # replay them as a single delta so callers see the full JSON.
            buffered = str(builder.get("arguments", ""))
            if buffered:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_DELTA,
                    tool_call_id=str(builder["id"]),
                    tool_name=str(builder["name"]),
                    partial_json=buffered,
                )

    # --- Request Building ---

    def _build_request_body(self, request: AIRequest) -> dict[str, Any]:
        model = request.model or self._model
        body: dict[str, Any] = {
            "model": model,
            "max_completion_tokens": self._max_tokens,
            "messages": self._build_messages(request.messages, request.system_prompt),
        }

        if request.tools:
            body["tools"] = self._build_tools(request.tools)

        # ``o``-series reasoning models only accept the default sampling
        # temperature; sending one explicitly causes a 400. Classic chat
        # models still honour the config value.
        if not _is_reasoning_model(model):
            body["temperature"] = self._temperature

        return body

    def _build_messages(
        self,
        messages: list[Message],
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        """Convert internal messages to OpenAI Chat Completions format.

        OpenAI puts the system prompt as the first ``{"role": "system"}``
        message rather than as a separate top-level field (that's the
        Anthropic shape). Tool results are user-adjacent ``{"role":
        "tool", "tool_call_id": ...}`` rows. Assistant tool calls live
        on an assistant message's ``tool_calls`` array with the
        arguments encoded as a JSON string (not a dict).
        """
        result: list[dict[str, Any]] = []

        if system_prompt:
            result.append({"role": "system", "content": system_prompt})

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                # Already injected above from AIRequest.system_prompt.
                # Historical system rows get merged as additional system
                # messages so their content doesn't get lost.
                if msg.content:
                    result.append({"role": "system", "content": msg.content})
                continue

            if msg.role == MessageRole.USER:
                result.append(self._build_user_message(msg))

            elif msg.role == MessageRole.ASSISTANT:
                # Slash-command turns are persisted as a single assistant
                # row carrying both ``tool_calls`` and ``tool_results``
                # (see AIService._slash_command_chat). OpenAI requires
                # the tool results to appear as separate ``tool``-role
                # rows after the assistant's tool_calls, so we split
                # such combined rows into the canonical sequence here.
                if msg.tool_calls and msg.tool_results:
                    result.append(
                        {
                            "role": "assistant",
                            "content": msg.content or None,
                            "tool_calls": [
                                self._encode_tool_call(tc) for tc in msg.tool_calls
                            ],
                        }
                    )
                    for tr in msg.tool_results:
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": tr.tool_call_id,
                                "content": tr.content,
                            }
                        )
                    continue

                assistant_row: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or None,
                }
                if msg.tool_calls:
                    assistant_row["tool_calls"] = [
                        self._encode_tool_call(tc) for tc in msg.tool_calls
                    ]
                result.append(assistant_row)

            elif msg.role == MessageRole.TOOL_RESULT:
                for tr in msg.tool_results:
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": tr.tool_call_id,
                            "content": tr.content,
                        }
                    )

        return result

    def _build_user_message(self, msg: Message) -> dict[str, Any]:
        """Build a user-role message, inlining any attachments.

        Order mirrors AnthropicAI: images, documents (as text stubs —
        OpenAI Chat Completions doesn't accept PDFs natively), text
        attachments, opaque file stubs, then the user's typed prompt.
        """
        if not msg.attachments:
            return {"role": "user", "content": msg.content}

        image_atts = [a for a in msg.attachments if a.kind == "image"]
        doc_atts = [a for a in msg.attachments if a.kind == "document"]
        text_atts = [a for a in msg.attachments if a.kind == "text"]
        ref_atts = [a for a in msg.attachments if a.kind == "file"]

        parts: list[dict[str, Any]] = []

        for img in image_atts:
            if img.data:
                data_url = f"data:{img.media_type};base64,{img.data}"
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    }
                )
            else:
                parts.append(
                    {
                        "type": "text",
                        "text": (
                            f"[Attached image: {img.name or 'image'} "
                            f"({img.media_type}, {img.size} bytes) — use "
                            f"read_workspace_file or run_workspace_script "
                            f"to access]"
                        ),
                    }
                )

        for doc in doc_atts:
            # OpenAI Chat Completions doesn't natively accept PDFs, so
            # every document becomes a text stub naming the file and
            # pointing the model at the workspace tools.
            parts.append(
                {
                    "type": "text",
                    "text": (
                        f"[Attached document: {doc.name or 'document'} "
                        f"({doc.media_type}, {doc.size} bytes) — use "
                        f"read_workspace_file or run_workspace_script "
                        f"to access]"
                    ),
                }
            )

        for txt in text_atts:
            if txt.text:
                parts.append(
                    {
                        "type": "text",
                        "text": f"## {txt.name}\n\n{txt.text}",
                    }
                )
            else:
                parts.append(
                    {
                        "type": "text",
                        "text": (
                            f"[Attached file: {txt.name or 'file'} "
                            f"({txt.media_type}, {txt.size} bytes) — use "
                            f"read_workspace_file or run_workspace_script "
                            f"to access]"
                        ),
                    }
                )

        for ref in ref_atts:
            parts.append(
                {
                    "type": "text",
                    "text": (
                        f"[Attached file: {ref.name or 'file'} "
                        f"({ref.media_type}, {ref.size} bytes) — use "
                        f"read_workspace_file or run_workspace_script "
                        f"to access]"
                    ),
                }
            )

        if msg.content:
            parts.append({"type": "text", "text": msg.content})

        if not parts:
            # Every attachment was an unknown kind — fall back to a plain
            # string message so the API gets a valid payload.
            return {"role": "user", "content": msg.content}

        return {"role": "user", "content": parts}

    @staticmethod
    def _encode_tool_call(tc: ToolCall) -> dict[str, Any]:
        """OpenAI expects ``tool_calls[i].function.arguments`` as a JSON
        string, not a parsed dict. Re-encode so the shape round-trips."""
        return {
            "id": tc.tool_call_id,
            "type": "function",
            "function": {
                "name": tc.tool_name,
                "arguments": json.dumps(tc.arguments),
            },
        }

    @staticmethod
    def _build_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert tool definitions to OpenAI function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.to_json_schema(),
                },
            }
            for tool in tools
        ]

    # --- Error & Response Parsing ---

    def _error_from_response(
        self,
        status: int,
        resp: httpx.Response,
        body: dict[str, Any],
    ) -> AIBackendError:
        """Build an ``AIBackendError`` from a non-streaming 4xx/5xx response.

        Pulls the human-readable reason out of OpenAI's error envelope
        (``{"error": {"message": "...", "type": "..."}}``) so the chat
        UI can surface it instead of opaque HTTP text.
        """
        err_body: Any
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text
        ai_logger.warning(
            "OpenAI API error: status=%d body=%s request=%s",
            status,
            err_body,
            json.dumps(body)[:2000],
        )
        reason = self._extract_error_reason(err_body)
        return AIBackendError(
            f"OpenAI API rejected request ({status}): {reason}",
            status=status,
        )

    def _error_from_stream_body(
        self,
        status: int,
        err_bytes: bytes,
    ) -> AIBackendError:
        """Build an ``AIBackendError`` from a streaming endpoint's error body."""
        try:
            err_body: Any = json.loads(err_bytes)
        except Exception:
            err_body = err_bytes.decode("utf-8", errors="replace")
        ai_logger.warning(
            "OpenAI stream API error: status=%d body=%s",
            status,
            err_body,
        )
        reason = self._extract_error_reason(err_body)
        return AIBackendError(
            f"OpenAI API rejected streaming request ({status}): {reason}",
            status=status,
        )

    @staticmethod
    def _extract_error_reason(err_body: Any) -> str:
        reason = ""
        if isinstance(err_body, dict):
            err_obj = err_body.get("error")
            if isinstance(err_obj, dict):
                reason = str(err_obj.get("message") or "").strip()
            if not reason:
                reason = str(err_body.get("message") or "").strip()
        if not reason:
            reason = str(err_body)[:500]
        return reason

    @staticmethod
    def _first_finish_reason(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            return str(choices[0].get("finish_reason") or "")
        return ""

    @staticmethod
    def _map_finish_reason(raw: str) -> StopReason:
        if raw == "tool_calls" or raw == "function_call":
            return StopReason.TOOL_USE
        if raw == "length":
            return StopReason.MAX_TOKENS
        return StopReason.END_TURN

    def _parse_response(self, data: dict[str, Any]) -> AIResponse:
        """Parse a Chat Completions response into an ``AIResponse``."""
        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        raw_content = message.get("content")
        content_text = raw_content if isinstance(raw_content, str) else ""

        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    tool_call_id=str(tc.get("id", "")),
                    tool_name=str(fn.get("name", "")),
                    arguments=args if isinstance(args, dict) else {},
                )
            )

        stop_reason = self._map_finish_reason(str(choice.get("finish_reason") or ""))

        usage = None
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            usage = TokenUsage(
                input_tokens=int(raw_usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
            )

        assistant_msg = Message(
            role=MessageRole.ASSISTANT,
            content=content_text,
            tool_calls=tool_calls,
        )

        return AIResponse(
            message=assistant_msg,
            model=str(data.get("model") or self._model),
            stop_reason=stop_reason,
            usage=usage,
        )
