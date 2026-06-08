"""Ollama AI backend — AI via a locally-running Ollama server.

`Ollama <https://ollama.com>`_ is a local LLM runtime that serves
open-weight models (Llama, Qwen, Mistral, DeepSeek, Gemma, Phi, …). We
talk to its **native** chat endpoint at ``{root}/api/chat`` rather than
the OpenAI-compatible ``/v1/chat/completions`` one (ADR-0009). The native
API is the only one that accepts a per-request context size
(``options.num_ctx``); the OpenAI shim has no field for it, so a model
loaded through it is stuck at the daemon default (often 4096 tokens) and
400s any larger prompt. Routing through ``/api/chat`` lets Gilbert load
the model with the context window resolved from per-model config.

No API key is required for local usage; remote/protected Ollama
instances can supply one anyway and it'll be passed through as a Bearer
token. ``base_url`` may be given with or without a trailing ``/v1`` — we
normalise to the daemon root either way.

Models are referenced by the tag a user has pulled locally (e.g.
``llama3.3``, ``qwen2.5-coder:32b``). The ``model`` field is free-text
so any pulled tag works without patching the plugin.
"""

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
    ToolResult,
)

from . import _installed_cache

logger = logging.getLogger(__name__)
ai_logger = logging.getLogger("gilbert.ai")

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.3"

# Recommended overlay: a curated set of common tool-capable model
# families, keyed by the base tag. ``available_models()`` is now
# **dynamic** — it reflects whatever tags are actually installed in the
# Ollama daemon (``GET /api/tags``). When an installed tag matches an
# entry here (exactly, or by family prefix), we borrow its friendly name
# + description; otherwise the tag is advertised as a bare
# ``ModelInfo(id=tag, name=tag)``. Per-model ``enabled`` filtering is now
# enforced by the core ``AIService`` (ADR-0019), not here.
_RECOMMENDED_OVERLAY = [
    ModelInfo(
        id="llama3.3",
        name="Llama 3.3 70B",
        description="Meta's flagship 70B chat model — strong tool use.",
    ),
    ModelInfo(
        id="llama3.2",
        name="Llama 3.2",
        description="Meta's smaller Llama 3.2 line (1B, 3B, 11B, 90B variants).",
    ),
    ModelInfo(
        id="qwen2.5",
        name="Qwen 2.5",
        description="Alibaba's open-weight chat line — sizes from 0.5B to 72B.",
    ),
    ModelInfo(
        id="qwen2.5-coder",
        name="Qwen 2.5 Coder",
        description="Code-specialized Qwen variant — strong at code tasks.",
    ),
    ModelInfo(
        id="deepseek-r1",
        name="DeepSeek R1",
        description="Reasoning-focused open-weight model distilled by DeepSeek.",
    ),
    ModelInfo(
        id="mistral",
        name="Mistral 7B",
        description="Mistral AI's open-weight 7B instruction model.",
    ),
    ModelInfo(
        id="mistral-nemo",
        name="Mistral Nemo 12B",
        description="Mistral's open-weight 12B model, Apache 2.0.",
    ),
    ModelInfo(
        id="phi4",
        name="Phi-4",
        description="Microsoft's compact reasoning model.",
    ),
    ModelInfo(
        id="gemma3",
        name="Gemma 3",
        description="Google's open-weight Gemma line — sizes from 1B to 27B.",
    ),
]


class OllamaAI(AIBackend):
    """AI backend using Ollama's native ``/api/chat`` endpoint (ADR-0009)."""

    backend_name = "ollama"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
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
                description=(
                    "Optional API key. Local Ollama doesn't require one; "
                    "leave blank. Populate only if your Ollama instance "
                    "is fronted by a reverse proxy that checks a Bearer "
                    "token."
                ),
                sensitive=True,
                default="",
            ),
            ConfigParam(
                key="base_url",
                type=ToolParameterType.STRING,
                description=(
                    "Ollama server URL (default ``http://localhost:11434``). "
                    "Point at another host/port if Ollama runs elsewhere. A "
                    "trailing ``/v1`` is accepted and stripped — Gilbert uses "
                    "the native ``/api`` endpoints."
                ),
                default=_DEFAULT_BASE_URL,
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description=(
                    "Default model tag (e.g. ``llama3.3``, ``qwen2.5:32b``). "
                    "Must be a model you've ``ollama pull``ed locally — "
                    "Ollama rejects unknown tags. Free-text because the set "
                    "depends on what you've pulled."
                ),
                default=_DEFAULT_MODEL,
            ),
            ConfigParam(
                key="max_tokens",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum tokens in a single AI response. Sent as "
                    "``options.num_predict`` to the Ollama endpoint."
                ),
                default=8192,
            ),
            ConfigParam(
                key="temperature",
                type=ToolParameterType.NUMBER,
                description=("Sampling temperature (0.0 = deterministic, 1.0 = creative)."),
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
                    "Send a tiny 'hi' message to the Ollama server to verify it's reachable and the model is available."
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
                message="Ollama backend is not initialized — save settings first.",
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
                message=f"Ollama API error: {exc}",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=f"Connected to Ollama (model: {response.model}).",
        )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = _DEFAULT_BASE_URL
        self._model: str = _DEFAULT_MODEL
        self._max_tokens: int = 8192
        self._temperature: float = 0.7
        # Per-model ``/api/show`` cache: ``{model: {"context_length": int|None,
        # "thinking": bool}}``. Populated lazily before a request (one show
        # call per model, then reused) so ``_build_request_body`` can clamp
        # ``num_ctx`` to the model's real max and only set ``think`` on models
        # that actually support it.
        self._model_info: dict[str, dict[str, Any]] = {}

    @property
    def _installed_tags(self) -> list[str]:
        """Installed Ollama tags, read from the host-global shared cache.

        Backed by ``_installed_cache`` rather than instance state so the
        runtime service's pull/delete writes are visible here immediately —
        that's what makes a freshly-pulled model chat-selectable without a
        backend restart.
        """
        return _installed_cache.get()

    @_installed_tags.setter
    def _installed_tags(self, tags: list[str]) -> None:
        _installed_cache.set(tags)

    async def initialize(self, config: dict[str, Any]) -> None:
        # ``api_key`` is intentionally optional: local Ollama doesn't
        # check authorization. A key is only needed when Ollama sits
        # behind a reverse proxy that gates access.
        api_key = str(config.get("api_key") or "").strip()

        self._model = str(config.get("model", _DEFAULT_MODEL))
        self._max_tokens = int(config.get("max_tokens", 8192))
        self._temperature = float(config.get("temperature", 0.7))
        base_url = str(config.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")

        headers: dict[str, str] = {
            "content-type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self._base_url = base_url
        # No ``base_url`` on the client: the native ``/api/*`` routes and the
        # legacy ``/v1`` shim live at different depths, so we build absolute
        # URLs from the normalised daemon root (``_root_url``) instead.
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=120.0,
        )
        # Seed the installed-tag cache so ``available_models()`` reflects
        # reality immediately. Best-effort: a daemon that's down at startup
        # just yields an empty list until the next refresh.
        await self.refresh_installed_models()
        logger.info(
            "Ollama AI backend initialized (model=%s, installed=%d)",
            self._model,
            len(self._installed_tags),
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def refresh_installed_models(self) -> None:
        """Refresh the cached set of installed Ollama tags from the daemon.

        Queries ``GET /api/tags`` (the daemon's native API, which sits at
        the server root — *not* under the OpenAI-compatible ``/v1`` path)
        and stores the tag names in the host-global ``_installed_cache`` so
        the sync ``available_models()`` can reflect them without doing
        network I/O. Best-effort: a daemon that's unreachable or returns an
        unexpected shape leaves the previous cache untouched.
        """
        if self._client is None:
            return
        try:
            resp = await self._client.get(self._tags_url())
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # daemon down / network / bad JSON
            logger.debug("Ollama /api/tags refresh failed: %s", exc)
            return
        if not isinstance(data, dict):
            return
        models = data.get("models")
        if not isinstance(models, list):
            return
        tags: list[str] = []
        for entry in models:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("model")
                if isinstance(name, str) and name:
                    tags.append(name)
        self._installed_tags = tags

    def _root_url(self) -> str:
        """The Ollama daemon root, with any trailing ``/v1`` shim stripped.

        ``base_url`` is accepted with or without ``/v1`` (the OpenAI shim
        path); the native ``/api/*`` routes — which we use exclusively — live
        at the root, so normalise here and build absolute URLs from it.
        """
        root = self._base_url
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        return root.rstrip("/")

    def _chat_url(self) -> str:
        """Absolute URL for the native ``/api/chat`` endpoint."""
        return f"{self._root_url()}/api/chat"

    def _tags_url(self) -> str:
        """Absolute URL for ``/api/tags`` at the Ollama daemon root."""
        return f"{self._root_url()}/api/tags"

    def _show_url(self) -> str:
        """Absolute URL for the native ``/api/show`` endpoint."""
        return f"{self._root_url()}/api/show"

    async def _ensure_model_info(self, model: str) -> dict[str, Any]:
        """Resolve + cache a model's ``context_length`` and thinking support.

        Queries ``POST /api/show`` once per model (then reuses the cache). The
        max context length lets us clamp ``num_ctx`` to what the model actually
        supports (a wildly oversized value makes Ollama allocate an impossible
        KV cache and the request hangs); ``capabilities`` tells us whether the
        model supports ``think`` so we only send it when it won't 400.

        Best-effort: a failed lookup caches an empty record so we don't retry
        every request, and the request proceeds without clamping / thinking.
        """
        cached = self._model_info.get(model)
        if cached is not None:
            return cached
        info: dict[str, Any] = {"context_length": None, "thinking": False}
        if self._client is not None:
            try:
                resp = await self._client.post(self._show_url(), json={"name": model})
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.debug("Ollama /api/show failed for %s: %s", model, exc)
                data = None
            if isinstance(data, dict):
                model_info = data.get("model_info")
                if isinstance(model_info, dict):
                    ctx = next(
                        (
                            v
                            for k, v in model_info.items()
                            if k.endswith(".context_length") and isinstance(v, (int, float))
                        ),
                        None,
                    )
                    if ctx is not None:
                        info["context_length"] = int(ctx)
                caps = data.get("capabilities")
                if isinstance(caps, list) and "thinking" in caps:
                    info["thinking"] = True
        self._model_info[model] = info
        return info

    def available_models(self) -> list[ModelInfo]:
        """Advertise one ``ModelInfo`` per **installed** Ollama tag.

        Sourced from the host-global ``_installed_cache`` (refreshed from
        the daemon's ``/api/tags`` here and after every runtime
        pull/delete). Each tag is enriched from the recommended overlay when
        it matches a curated family; otherwise it's advertised as a bare
        ``ModelInfo(id=tag, name=tag)``. Per-model ``enabled`` filtering is
        applied downstream by the core ``AIService`` (ADR-0019), so it's
        intentionally not done here.
        """
        return [self._model_info_for_tag(tag) for tag in self._installed_tags]

    @staticmethod
    def _model_info_for_tag(tag: str) -> ModelInfo:
        """Build a ``ModelInfo`` for an installed tag, enriched from overlay.

        Matches the recommended overlay exactly first, then by family
        prefix (an installed ``llama3.3:latest`` / ``qwen2.5-coder:32b``
        borrows the friendly name of the ``llama3.3`` / ``qwen2.5-coder``
        overlay entry). The advertised ``id`` is always the real installed
        tag so chat selection round-trips to Ollama verbatim.
        """
        base = tag.split(":", 1)[0]
        for rec in _RECOMMENDED_OVERLAY:
            if rec.id == tag or rec.id == base:
                return ModelInfo(id=tag, name=rec.name, description=rec.description)
        return ModelInfo(id=tag, name=tag)

    def capabilities(self) -> AIBackendCapabilities:
        return AIBackendCapabilities(
            streaming=True,
            attachments_user=True,
            parallel_tool_calls=True,
        )

    async def generate(self, request: AIRequest) -> AIResponse:
        if self._client is None:
            raise RuntimeError("OllamaAI not initialized")

        await self._ensure_model_info(request.model or self._model)
        body = self._build_request_body(request)
        body["stream"] = False

        ai_logger.debug(
            "Ollama request: model=%s messages=%d num_ctx=%s think=%s",
            body["model"],
            len(body["messages"]),
            body.get("options", {}).get("num_ctx"),
            body.get("think", False),
        )

        try:
            resp = await self._client.post(self._chat_url(), json=body)
        except httpx.TimeoutException as exc:
            raise self._timeout_error(body, exc) from exc
        except httpx.TransportError as exc:
            raise AIBackendError(f"Ollama request failed: {exc}", status=502) from exc
        if resp.is_error:
            raise self._error_from_response(resp.status_code, resp, body)
        data = resp.json()

        ai_logger.debug(
            "Ollama response: done_reason=%s prompt_eval=%s eval=%s",
            data.get("done_reason"),
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )

        return self._parse_response(data)

    async def generate_stream(
        self,
        request: AIRequest,
    ) -> AsyncIterator[StreamEvent]:
        """Stream Ollama's native ``/api/chat`` NDJSON as ``StreamEvent``s.

        Native streaming emits one JSON object per line (not OpenAI SSE):
        each carries a ``message`` with a ``content`` delta and, on the final
        line, ``done: true`` plus ``done_reason`` and token counts
        (``prompt_eval_count`` / ``eval_count``). Tool calls arrive *complete*
        in a single chunk's ``message.tool_calls`` — Ollama doesn't stream
        tool arguments token-by-token — so each is surfaced as a
        ``TOOL_CALL_START`` + ``TOOL_CALL_END`` pair and folded into the final
        ``AIResponse`` carried by ``MESSAGE_COMPLETE``.
        """
        if self._client is None:
            raise RuntimeError("OllamaAI not initialized")

        await self._ensure_model_info(request.model or self._model)
        body = self._build_request_body(request)
        body["stream"] = True

        ai_logger.debug(
            "Ollama stream request: model=%s messages=%d num_ctx=%s think=%s",
            body["model"],
            len(body["messages"]),
            body.get("options", {}).get("num_ctx"),
            body.get("think", False),
        )

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        done_reason = "stop"
        usage_input = 0
        usage_output = 0
        model_id = str(body["model"])

        try:
            async with self._client.stream(
                "POST",
                self._chat_url(),
                json=body,
            ) as resp:
                if resp.is_error:
                    err_bytes = await resp.aread()
                    raise self._error_from_stream_body(
                        resp.status_code,
                        err_bytes,
                    )

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue

                    model_id = str(data.get("model") or model_id)

                    message = data.get("message")
                    if isinstance(message, dict):
                        # Reasoning (``think`` models) streams in a separate
                        # ``thinking`` field, distinct from the answer ``content``.
                        thinking = message.get("thinking")
                        if isinstance(thinking, str) and thinking:
                            reasoning_parts.append(thinking)
                            yield StreamEvent(
                                type=StreamEventType.REASONING_DELTA,
                                text=thinking,
                            )
                        content = message.get("content")
                        if isinstance(content, str) and content:
                            text_parts.append(content)
                            yield StreamEvent(
                                type=StreamEventType.TEXT_DELTA,
                                text=content,
                            )
                        raw_tool_calls = message.get("tool_calls")
                        if isinstance(raw_tool_calls, list):
                            for tc in self._parse_tool_calls(
                                raw_tool_calls, start=len(tool_calls)
                            ):
                                tool_calls.append(tc)
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL_START,
                                    tool_call_id=tc.tool_call_id,
                                    tool_name=tc.tool_name,
                                )
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL_END,
                                    tool_call_id=tc.tool_call_id,
                                    tool_name=tc.tool_name,
                                )

                    if data.get("done"):
                        done_reason = str(data.get("done_reason") or done_reason)
                        usage_input = int(data.get("prompt_eval_count", usage_input) or 0)
                        usage_output = int(data.get("eval_count", usage_output) or 0)
        except httpx.TimeoutException as exc:
            raise self._timeout_error(body, exc) from exc
        except httpx.TransportError as exc:
            raise AIBackendError(f"Ollama request failed: {exc}", status=502) from exc

        stop_reason = self._stop_reason(done_reason, bool(tool_calls))

        final_message = Message(
            role=MessageRole.ASSISTANT,
            content="".join(text_parts),
            reasoning="".join(reasoning_parts),
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
            "Ollama stream response: done_reason=%s usage=%s",
            done_reason,
            {"prompt_eval_count": usage_input, "eval_count": usage_output},
        )
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            response=final_response,
        )

    @staticmethod
    def _parse_tool_calls(raw: list[Any], start: int = 0) -> list[ToolCall]:
        """Convert native ``message.tool_calls`` entries into ``ToolCall``s.

        Native tool calls are ``{"function": {"name", "arguments": <object>}}``
        — arguments is already a JSON object (not the OpenAI JSON *string*) and
        there is no ``id``, so we synthesise a per-turn id (``call_<n>``) for
        the loop's call/result bookkeeping. The synthetic id never goes back on
        the wire: native tool-result messages are matched by order + tool name,
        not id.
        """
        out: list[ToolCall] = []
        for offset, tc in enumerate(raw):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            name = str(fn.get("name") or "")
            if not name:
                continue
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str) and raw_args:
                try:
                    parsed = json.loads(raw_args)
                except json.JSONDecodeError:
                    parsed = {}
                args = parsed if isinstance(parsed, dict) else {}
            else:
                args = {}
            tc_id = str(tc.get("id") or f"call_{start + offset}")
            out.append(ToolCall(tool_call_id=tc_id, tool_name=name, arguments=args))
        return out

    @staticmethod
    def _stop_reason(done_reason: str, has_tool_calls: bool) -> StopReason:
        """Map a native ``done_reason`` (+ tool-call presence) to ``StopReason``."""
        if has_tool_calls:
            return StopReason.TOOL_USE
        if done_reason == "length":
            return StopReason.MAX_TOKENS
        return StopReason.END_TURN

    # --- Request Building ---

    def _build_request_body(self, request: AIRequest) -> dict[str, Any]:
        model = request.model or self._model
        # Layered generation params (ADR-0019): the request carries the
        # already-resolved per-model / profile / per-call values when set;
        # ``None`` means "use this backend's own global default".
        max_tokens = request.max_tokens if request.max_tokens is not None else self._max_tokens
        temperature = request.temperature if request.temperature is not None else self._temperature

        # Native ``/api/chat`` carries generation params under ``options``:
        # ``num_predict`` is the OpenAI ``max_tokens``, ``num_ctx`` is the
        # context window. ``num_ctx`` is the whole reason we use this endpoint
        # — it loads the model with the caller-resolved window instead of the
        # daemon default. Omit it when unset so Ollama keeps its own default.
        options: dict[str, Any] = {
            "temperature": temperature,
            "num_predict": max_tokens,
        }
        if request.context_window is not None:
            # Clamp to the model's real max (from /api/show, when known): a
            # value beyond it makes Ollama try to allocate an impossible KV
            # cache and the request hangs instead of returning an error.
            model_max = self._model_info.get(model, {}).get("context_length")
            num_ctx = request.context_window
            if isinstance(model_max, int) and model_max > 0:
                num_ctx = min(num_ctx, model_max)
            options["num_ctx"] = num_ctx

        body: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(request.messages, request.system_prompt),
            "options": options,
        }

        # Ask for reasoning only on models that advertise it (per /api/show);
        # sending ``think`` to a non-thinking model 400s.
        if self._model_info.get(model, {}).get("thinking"):
            body["think"] = True

        if request.tools:
            body["tools"] = self._build_tools(request.tools)

        return body

    def _build_messages(
        self,
        messages: list[Message],
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        """Convert internal messages to native ``/api/chat`` format.

        Native tool results are ``{"role": "tool", "content": …}`` with an
        optional ``tool_name`` (there is no ``tool_call_id`` on the wire —
        calls and results are matched by order). ``ToolResult`` only stores the
        id, so we first build an id→name map across the whole history to fill
        ``tool_name`` even for standalone ``TOOL_RESULT`` rows.
        """
        name_by_id: dict[str, str] = {}
        for msg in messages:
            for tc in msg.tool_calls:
                if tc.tool_call_id:
                    name_by_id[tc.tool_call_id] = tc.tool_name

        result: list[dict[str, Any]] = []

        if system_prompt:
            result.append({"role": "system", "content": system_prompt})

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                if msg.content:
                    result.append({"role": "system", "content": msg.content})
                continue

            if msg.role == MessageRole.USER:
                result.append(self._build_user_message(msg))

            elif msg.role == MessageRole.ASSISTANT:
                # Slash-command turns are persisted as a single assistant row
                # carrying both ``tool_calls`` and ``tool_results``; Ollama
                # requires the results to follow as separate ``tool`` rows
                # after the assistant's tool_calls, so split them here.
                result.append(self._assistant_row(msg))
                if msg.tool_calls and msg.tool_results:
                    for tr in msg.tool_results:
                        result.append(self._tool_result_row(tr, name_by_id))

            elif msg.role == MessageRole.TOOL_RESULT:
                for tr in msg.tool_results:
                    result.append(self._tool_result_row(tr, name_by_id))

        return result

    @staticmethod
    def _assistant_row(msg: Message) -> dict[str, Any]:
        """Native assistant row: ``content`` plus optional ``tool_calls``."""
        row: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            row["tool_calls"] = [OllamaAI._encode_tool_call(tc) for tc in msg.tool_calls]
        return row

    @staticmethod
    def _tool_result_row(tr: ToolResult, name_by_id: dict[str, str]) -> dict[str, Any]:
        """Native tool-result row: ``{"role": "tool", "content", tool_name?}``."""
        row: dict[str, Any] = {"role": "tool", "content": tr.content}
        name = name_by_id.get(tr.tool_call_id)
        if name:
            row["tool_name"] = name
        return row

    def _build_user_message(self, msg: Message) -> dict[str, Any]:
        """Build a native user message: text ``content`` + optional ``images``.

        Multimodal Ollama models (``llava``, ``llama3.2-vision``,
        ``qwen2.5-vl``, …) take raw base64 image strings in an ``images`` array
        on the message — no ``data:`` URL prefix and no OpenAI content-parts
        array. Non-image attachments (documents, text without inline content,
        file references, and image refs without inline bytes) are described in
        the text so the model knows to fetch them via a workspace tool.
        """
        if not msg.attachments:
            return {"role": "user", "content": msg.content}

        images: list[str] = []
        text_blocks: list[str] = []

        for att in msg.attachments:
            if att.kind == "image":
                if att.data:
                    images.append(att.data)
                else:
                    text_blocks.append(
                        f"[Attached image: {att.name or 'image'} "
                        f"({att.media_type}, {att.size} bytes) — use "
                        f"read_workspace_file or run_workspace_script to access]"
                    )
            elif att.kind == "document":
                text_blocks.append(
                    f"[Attached document: {att.name or 'document'} "
                    f"({att.media_type}, {att.size} bytes) — use "
                    f"read_workspace_file or run_workspace_script to access]"
                )
            elif att.kind == "text" and att.text:
                text_blocks.append(f"## {att.name}\n\n{att.text}")
            else:
                text_blocks.append(
                    f"[Attached file: {att.name or 'file'} "
                    f"({att.media_type}, {att.size} bytes) — use "
                    f"read_workspace_file or run_workspace_script to access]"
                )

        if msg.content:
            text_blocks.append(msg.content)

        row: dict[str, Any] = {"role": "user", "content": "\n\n".join(text_blocks)}
        if images:
            row["images"] = images
        return row

    @staticmethod
    def _encode_tool_call(tc: ToolCall) -> dict[str, Any]:
        """Native ``message.tool_calls[i]`` — ``arguments`` is a JSON object.

        Unlike the OpenAI shim (which wants ``arguments`` as a JSON *string*
        and an ``id``), the native API takes the arguments object directly and
        has no id field.
        """
        return {
            "function": {
                "name": tc.tool_name,
                "arguments": tc.arguments,
            },
        }

    @staticmethod
    def _build_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert tool definitions to the native function-calling format.

        Native ``/api/chat`` uses the same ``{"type": "function", "function":
        {name, description, parameters}}`` shape as OpenAI.
        """
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

    def _timeout_error(self, body: dict[str, Any], exc: Exception) -> AIBackendError:
        """A clear ``AIBackendError`` for an httpx timeout, not a bare error.

        The usual culprit is an oversized ``num_ctx`` — Ollama hangs trying to
        allocate a KV cache the host can't fit — so name it in the message
        instead of letting the chat surface an opaque "Unknown error".
        """
        num_ctx = body.get("options", {}).get("num_ctx")
        ai_logger.warning(
            "Ollama request timed out (model=%s, num_ctx=%s): %s",
            body.get("model"),
            num_ctx,
            exc,
        )
        hint = ""
        if num_ctx:
            hint = (
                f" The request used num_ctx={num_ctx}; if that's large the host "
                "may not be able to allocate it — lower the model's context window."
            )
        return AIBackendError(
            "Ollama request timed out — the model may still be loading, or its "
            f"context window is too large for available memory.{hint}",
            status=504,
        )

    def _error_from_response(
        self,
        status: int,
        resp: httpx.Response,
        body: dict[str, Any],
    ) -> AIBackendError:
        err_body: Any
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text
        ai_logger.warning(
            "Ollama API error: status=%d body=%s request=%s",
            status,
            err_body,
            json.dumps(body)[:2000],
        )
        reason = self._extract_error_reason(err_body)
        return AIBackendError(
            f"Ollama API rejected request ({status}): {reason}",
            status=status,
        )

    def _error_from_stream_body(
        self,
        status: int,
        err_bytes: bytes,
    ) -> AIBackendError:
        try:
            err_body: Any = json.loads(err_bytes)
        except Exception:
            err_body = err_bytes.decode("utf-8", errors="replace")
        ai_logger.warning(
            "Ollama stream API error: status=%d body=%s",
            status,
            err_body,
        )
        reason = self._extract_error_reason(err_body)
        return AIBackendError(
            f"Ollama API rejected streaming request ({status}): {reason}",
            status=status,
        )

    @staticmethod
    def _extract_error_reason(err_body: Any) -> str:
        # The native API returns ``{"error": "<message string>"}``; the OpenAI
        # shim returned ``{"error": {"message": …}}``. Handle both shapes.
        reason = ""
        if isinstance(err_body, dict):
            err_obj = err_body.get("error")
            if isinstance(err_obj, str):
                reason = err_obj.strip()
            elif isinstance(err_obj, dict):
                reason = str(err_obj.get("message") or "").strip()
            if not reason:
                reason = str(err_body.get("message") or "").strip()
        if not reason:
            reason = str(err_body)[:500]
        return reason

    def _parse_response(self, data: dict[str, Any]) -> AIResponse:
        """Parse a native ``/api/chat`` (non-stream) response into ``AIResponse``."""
        message = data.get("message")
        if not isinstance(message, dict):
            message = {}
        raw_content = message.get("content")
        content_text = raw_content if isinstance(raw_content, str) else ""
        raw_thinking = message.get("thinking")
        reasoning_text = raw_thinking if isinstance(raw_thinking, str) else ""

        raw_tool_calls = message.get("tool_calls")
        tool_calls = (
            self._parse_tool_calls(raw_tool_calls) if isinstance(raw_tool_calls, list) else []
        )

        stop_reason = self._stop_reason(str(data.get("done_reason") or ""), bool(tool_calls))

        usage = None
        if "prompt_eval_count" in data or "eval_count" in data:
            usage = TokenUsage(
                input_tokens=int(data.get("prompt_eval_count", 0) or 0),
                output_tokens=int(data.get("eval_count", 0) or 0),
            )

        assistant_msg = Message(
            role=MessageRole.ASSISTANT,
            content=content_text,
            reasoning=reasoning_text,
            tool_calls=tool_calls,
        )

        return AIResponse(
            message=assistant_msg,
            model=str(data.get("model") or self._model),
            stop_reason=stop_reason,
            usage=usage,
        )
