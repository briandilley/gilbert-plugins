"""Live integration tests against a real local Ollama daemon.

Unlike ``test_ollama_ai.py`` (which mocks the HTTP transport), these hit an
actual Ollama at ``http://localhost:11434`` and run the operator's real
reasoning model end-to-end through ``OllamaAI`` — the same code path the chat
loop uses. They reproduce the conditions that previously failed with
``AIBackendError: Ollama request timed out`` (a thinking model whose cold load
+ reasoning exceeded the old hardcoded 120s) and assert it now completes.

The settings mirror the operator's saved config: defaults everywhere
(``base_url`` localhost, ``max_tokens`` 8192, ``temperature`` 0.7), with
``context_window`` left unset so ``num_ctx`` is the daemon default and ``think``
is auto-enabled for a model that advertises the ``thinking`` capability.

Skipped automatically when the daemon is unreachable or the model isn't pulled,
so the suite stays green on machines without Ollama. Marked ``slow`` because a
cold load plus a reasoning generation can take minutes on CPU-only hardware —
run with ``uv run pytest -m slow std-plugins/ollama/tests/test_ollama_integration.py``.
"""

from __future__ import annotations

import httpx
import pytest
from gilbert_plugin_ollama.ollama_ai import OllamaAI

from gilbert.interfaces.ai import AIRequest, Message, MessageRole, StreamEventType

OLLAMA_URL = "http://localhost:11434"
# The reasoning model the operator has installed (capabilities include
# "thinking" + "tools"). 8.95B params, BF16 (~18GB), 262k context.
MODEL = "hf.co/Jackrong/Qwen3.5-9B-DeepSeek-V4-Flash-GGUF:BF16"


def _daemon_has_model(url: str, model: str) -> bool:
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=5.0)
        resp.raise_for_status()
        tags = {m.get("name") for m in resp.json().get("models", [])}
    except Exception:
        return False
    return model in tags


_AVAILABLE = _daemon_has_model(OLLAMA_URL, MODEL)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _AVAILABLE,
        reason=(
            f"local Ollama at {OLLAMA_URL} with model {MODEL} not available — "
            "pull the model and start the daemon to run live integration tests"
        ),
    ),
]


def _operator_settings() -> dict[str, object]:
    """The operator's real backend config, plus a generous ``request_timeout``
    so a cold 18GB CPU-only load isn't cut off mid-flight."""
    return {
        "base_url": OLLAMA_URL,
        "model": MODEL,
        # 15 minutes: comfortably covers a cold load + reasoning on CPU.
        "request_timeout": 900,
    }


async def test_generate_completes_with_real_reasoning_model() -> None:
    """A non-streaming generation against the real model returns an answer
    instead of timing out — this is the exact failure the operator reported."""
    backend = OllamaAI()
    await backend.initialize(_operator_settings())
    try:
        request = AIRequest(
            model=MODEL,
            messages=[
                Message(
                    role=MessageRole.USER,
                    content="What is 2 + 2? Reply with just the number.",
                )
            ],
            system_prompt="You are concise.",
            max_tokens=2048,
        )
        response = await backend.generate(request)
    finally:
        await backend.close()

    assert response.message.content.strip(), "model returned empty content"
    assert "4" in response.message.content


async def test_generate_stream_yields_reasoning_then_content() -> None:
    """The streaming path surfaces the model's thinking as REASONING_DELTA
    events (distinct from TEXT_DELTA content) and finishes with a
    MESSAGE_COMPLETE — verifying the reasoning-streaming path works against a
    real thinking model without timing out."""
    backend = OllamaAI()
    await backend.initialize(_operator_settings())

    saw_reasoning = False
    saw_text = False
    completed = False
    final_content = ""
    try:
        request = AIRequest(
            model=MODEL,
            messages=[Message(role=MessageRole.USER, content="Briefly, what is 2 + 2?")],
            max_tokens=2048,
        )
        async for event in backend.generate_stream(request):
            if event.type == StreamEventType.REASONING_DELTA:
                saw_reasoning = True
            elif event.type == StreamEventType.TEXT_DELTA:
                saw_text = True
            elif event.type == StreamEventType.MESSAGE_COMPLETE:
                completed = True
                if event.response is not None:
                    final_content = event.response.message.content
    finally:
        await backend.close()

    assert saw_text, "expected content (TEXT_DELTA) tokens from the model"
    assert completed, "stream must end with a MESSAGE_COMPLETE event"
    # This model advertises the "thinking" capability, so the backend
    # auto-enables ``think`` and we expect at least some reasoning deltas.
    assert saw_reasoning, "expected reasoning (thinking) deltas from a thinking model"
    assert "4" in final_content
