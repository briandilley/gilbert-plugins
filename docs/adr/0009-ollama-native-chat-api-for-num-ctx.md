# Ollama backend talks to the native `/api/chat`, not the OpenAI `/v1` shim, to set `num_ctx`

The `ollama` `AIBackend` sends chat requests to Ollama's **native** `/api/chat` endpoint, carrying
generation settings under `options` (`temperature`, `num_predict` = our `max_tokens`, and crucially
`num_ctx` = the resolved per-model **context window**). It previously spoke the OpenAI-compatible
`/v1/chat/completions` shim.

The driver is `num_ctx`. Ollama loads a model with a fixed context size and the OpenAI shim has **no
field to set it** — [the official docs say so explicitly](https://docs.ollama.com/api/openai-compatibility)
and list the supported fields (no context knob). So a model served through `/v1` is pinned to the
daemon default (commonly 4096 tokens) regardless of what Gilbert thinks the window is, and any prompt
larger than that gets a hard `400 … exceeds the available context size`. The native API is the only
per-request way to load the model with a chosen window, so Gilbert routes through it and maps the
context window resolved by `AIService` (per-model config layer, core ADR-0019) onto `options.num_ctx`.

`AIRequest` gained a `context_window: int | None` field (core change) that the layered resolver fills
from `ModelConfig.context_window`; `AIService` also trims history to fit that window before the call
(only when it's set — hosted backends leave it `None` and are unaffected). `context_window` is seeded
on pull from HF metadata and editable on the per-model config page.

## Considered options

- **Keep `/v1/chat/completions`, set context another way** — rejected. The shim can't take `num_ctx`.
  The remaining levers are out-of-band and worse: the `OLLAMA_CONTEXT_LENGTH` env var is daemon-global
  (not per-model, and outside Gilbert's control — it's the operator's systemd unit), and a per-model
  `Modelfile` + `ollama create` variant is stateful bookkeeping Gilbert would have to own and keep in
  sync with each pulled tag.
- **A top-level `num_ctx` on the `/v1` request** — some third-party guides claim it works, but the
  official docs say the OpenAI endpoint doesn't support it; we don't build on undocumented behaviour.

## Consequences

The native API has a different wire shape than the OpenAI shim, so the backend's request/response and
streaming parsing were rewritten:

- **Requests**: `messages` carry images as a raw-base64 `images` array (not `image_url` data-URL
  content parts); generation params live under `options`.
- **Tool calls**: native `message.tool_calls[i].function.arguments` is a JSON **object** (the shim
  used a JSON *string*) and there is **no `id`** — calls and results are matched by order, not id. The
  backend synthesises an internal `tool_call_id` for the agentic loop's bookkeeping and never puts it
  back on the wire; tool results are sent as `{"role": "tool", "content", tool_name}` rows.
- **Streaming**: native streaming is newline-delimited JSON objects (not OpenAI SSE `data:` frames),
  each with a `message.content` delta and, on the final line, `done` + `done_reason` +
  `prompt_eval_count` / `eval_count`. Tool calls arrive complete in one chunk (Ollama doesn't stream
  tool-argument deltas).

`base_url` is accepted with or without a trailing `/v1` and normalised to the daemon root, so configs
carried over from the old default keep working. The `local_model_runtime` service already used the
native `/api/*` routes, so it is unchanged.
