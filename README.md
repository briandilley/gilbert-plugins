# Gilbert Plugins

First-party plugins for the [Gilbert](https://github.com/briandilley/gilbert) AI assistant.

This repository is cloned into `std-plugins/` inside a Gilbert checkout (as a git submodule) and each subdirectory here is loaded automatically at Gilbert startup. Every plugin is **self-contained** — it declares its own Python dependencies in its own `pyproject.toml`, registers its backends or services when loaded, and can be enabled, disabled, and configured entirely from the Gilbert Settings UI without editing any files.

## How to use this repository

You don't normally interact with this repo directly. Gilbert's `gilbert.sh start` runs `git submodule update --init --recursive` if the `std-plugins/` directory is empty, then `uv sync` — which walks every plugin's `pyproject.toml`, installs its third-party deps into Gilbert's shared venv, and leaves the plugin ready to load.

To hack on a plugin:

```bash
cd std-plugins/<plugin-name>
# edit files, run tests from the gilbert repo root
cd ../..
uv run pytest std-plugins/<plugin-name>/tests/ -v
```

To add a new plugin, see the [Adding a Plugin](#adding-a-plugin) section below.

## Available plugins

The table below is an index — jump to each plugin's detail section for configuration, slash commands, and notes.

| Plugin | Provides | Third-party deps | Category |
|---|---|---|---|
| [anthropic](#anthropic) | `AIBackend "anthropic"`, `VisionBackend "anthropic"` | `anthropic` | Intelligence |
| [arr](#arr) | `radarr` service, `sonarr` service | — (uses `httpx`) | Media |
| [bedrock](#bedrock) | `AIBackend "bedrock"` | `boto3` | Intelligence |
| [deepseek](#deepseek) | `AIBackend "deepseek"` | — (uses `httpx`) | Intelligence |
| [elevenlabs](#elevenlabs) | `TTSBackend "elevenlabs"` | — (uses `httpx`) | Media |
| [gemini](#gemini) | `AIBackend "gemini"` | — (uses `httpx`) | Intelligence |
| [google](#google) | `AuthBackend "google"`, `UserProviderBackend "google_directory"`, `EmailBackend "gmail"`, `DocumentBackend "google_drive"` | `google-auth`, `google-api-python-client` | Identity / Communication / Knowledge |
| [groq](#groq) | `AIBackend "groq"` | — (uses `httpx`) | Intelligence |
| [guess-that-song](#guess-that-song) | `guess_game` service | — (pure stdlib) | Games |
| [mistral](#mistral) | `AIBackend "mistral"` | — (uses `httpx`) | Intelligence |
| [ngrok](#ngrok) | `TunnelBackend "ngrok"` | `pyngrok` | Infrastructure |
| [ollama](#ollama) | `AIBackend "ollama"` | — (uses `httpx`) | Intelligence |
| [openai](#openai) | `AIBackend "openai"` | — (uses `httpx`) | Intelligence |
| [openrouter](#openrouter) | `AIBackend "openrouter"` | — (uses `httpx`) | Intelligence |
| [qwen](#qwen) | `AIBackend "qwen"` | — (uses `httpx`) | Intelligence |
| [slack](#slack) | `slack` service (Socket Mode bot) | `slack-bolt` | Communication |
| [sonos](#sonos) | `SpeakerBackend "sonos"`, `MusicBackend "sonos"` | `soco` | Media |
| [tavily](#tavily) | `WebSearchBackend "tavily"` | — (uses `httpx`) | Intelligence |
| [tesseract](#tesseract) | `OCRBackend "tesseract"` | `pytesseract` | Intelligence |
| [unifi](#unifi) | `PresenceBackend "unifi"`, `DoorbellBackend "unifi"` | — (uses `httpx`/`aiohttp`) | Monitoring |
| [xai](#xai) | `AIBackend "xai"` | — (uses `httpx`) | Intelligence |

---

### anthropic

Claude-powered AI chat and vision backends, speaking the Anthropic Messages API directly over `httpx` (no SDK import for the chat backend; the vision backend lazily imports `anthropic` for its one helper call).

**Backends registered**
- `AIBackend.backend_name = "anthropic"` — tool-use capable, streaming, per-call model override.
- `VisionBackend.backend_name = "anthropic"` — image understanding via Claude's vision API.

**Configure** (Settings → AI and Settings → Vision)
- `enabled` — Initialize this backend at startup (default `true`). Uncheck to hide its settings and stop it being offered in profile dropdowns.
- `api_key` *(sensitive)* — Anthropic API key (`sk-ant-…`).
- `model` — Default Claude model ID used when a request specifies no per-call model (default `claude-sonnet-4-20250514` for chat, `claude-sonnet-4-5-20250929` for vision).
- `enabled_models` — Subset of advertised models that the chat UI and AI profile editor expose for selection. Defaults to every model the backend knows about.
- `max_tokens` — Per-response cap (default `16384`). Sonnet/Opus 4.x comfortably support higher; the AIService recovers from a `max_tokens` cut-off on a text-only response via bounded continuation, but a `tool_use` that gets truncated mid-JSON is unrecoverable, so keep this comfortably above the largest tool input you expect.
- `temperature` — Sampling temperature (chat only).

**Streaming.** The chat backend implements `generate_stream` over SSE — `AIService` forwards each text chunk as a `chat.stream.text_delta` event on the bus, plus `chat.stream.round_complete` after every AI round and `chat.stream.turn_complete` at the end. The WS layer delivers them to the conversation's audience (owner for personal chats, members for shared rooms). The frontend's `TurnBubble` builds a live "thinking card" inside the in-flight turn from those events plus `chat.tool.started` / `chat.tool.completed`, and commits to the authoritative round structure when the `chat.message.send` RPC resolves with the server's `rounds` field. All Anthropic-specific SSE parsing stays inside `anthropic_ai.py`; `capabilities()` reports `streaming=True, attachments_user=True`.

**Config action** — `test_connection`: issues a one-token completion to verify credentials.

---

### arr

Radarr + Sonarr integration for browsing, searching, and managing your movie and TV library from Gilbert chat. Registered as two services (`radarr`, `sonarr`) so you can run either independently.

**Slash commands** (both services use the same verbs, prefixed `/radarr` or `/sonarr`)
- `list`, `find`, `search`, `details`, `grab`, `add`, `remove`
- `profiles`, `queue`, `recent`, `upcoming`
- `episodes` *(sonarr only)*

**Configure** (Settings → Media → Radarr / Sonarr)
- `url` — Radarr/Sonarr base URL (e.g., `http://radarr.lan:7878`).
- `api_key` *(sensitive)* — instance API key.
- `default_quality_profile` — Quality profile name or ID to use when adding new items.
- `default_root_folder` — Root folder path for new downloads.

**Requires**: nothing on the Gilbert side beyond `httpx`, which is already a core dep.

---

### bedrock

AWS Bedrock chat backend — unlike every other AI plugin this one doesn't speak an OpenAI-compatible API. Bedrock's [Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html) gives us a unified request shape across Anthropic Claude, Meta Llama, Mistral, and Amazon Nova models, with AWS SigV4 authentication. Useful for installations that already run on AWS and want their model traffic to stay in-VPC / billed through AWS.

**Backend registered** — `AIBackend.backend_name = "bedrock"`: tool-use capable, streaming via `converse_stream`, image-input capable on vision-capable models (Claude, Nova), per-call model override.

**Configure** (Settings → Intelligence → AI, with the `bedrock` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `aws_region` — AWS region for the Bedrock runtime endpoint (default `us-east-1`). Cross-region inference-profile IDs (`us.` / `eu.` prefixed) route automatically within the partition.
- `aws_access_key_id` — Optional. Leave blank to use boto3's default credential chain (env vars, `~/.aws/credentials`, EC2/ECS/Lambda IAM role).
- `aws_secret_access_key` *(sensitive)* — Optional. Paired with the access key.
- `aws_session_token` *(sensitive)* — Optional. For temporary credentials (STS AssumeRole, SSO).
- `model` — Default Bedrock model ID or inference profile ID (default `us.anthropic.claude-sonnet-4-5-20250929-v1:0`). Free-text because the available catalog varies per account and region — paste any model ID from the Bedrock console.
- `enabled_models` — Suggested subset shown in the chat UI and AI profile editor. Ships with common Claude / Llama / Mistral / Nova IDs.
- `max_tokens` — Per-response cap (default `8192`). Sent as `inferenceConfig.maxTokens`.
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** The backend drives `converse_stream`'s blocking iterator in a background thread and forwards events onto an `asyncio.Queue`. The main coroutine consumes the queue and maps `contentBlockStart` / `contentBlockDelta` / `contentBlockStop` / `messageStop` / `metadata` events to neutral `StreamEvent`s — `TEXT_DELTA`, `TOOL_CALL_START`, `TOOL_CALL_DELTA`, `TOOL_CALL_END`, and finally `MESSAGE_COMPLETE` with the assembled `AIResponse`.

**Attachments.** Vision-capable Bedrock models (Claude, Nova) accept image content blocks with raw bytes (not base64 strings — the plugin decodes). Supported formats: `png`, `jpeg`, `gif`, `webp`. Documents and text attachments become text stubs pointing the model at the workspace tools.

**Config action** — `test_connection`: issues a one-word completion to verify credentials and region.

**Third-party deps**: `boto3` (for AWS SigV4 signing, credential resolution, and the Converse / ConverseStream APIs).

---

### deepseek

DeepSeek chat backend, speaking the [OpenAI-compatible DeepSeek API](https://api-docs.deepseek.com/) directly over `httpx`. Runs alongside the other AI backends — pick per-profile in the AI profile editor.

**Backend registered** — `AIBackend.backend_name = "deepseek"`: tool-use capable, streaming, per-call model override.

**Configure** (Settings → Intelligence → AI, with the `deepseek` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — DeepSeek API key (`sk-…`).
- `base_url` — API base URL (default `https://api.deepseek.com/v1`).
- `model` — Default model ID (default `deepseek-chat`). Choices: `deepseek-chat` (DeepSeek V3), `deepseek-reasoner` (DeepSeek R1).
- `enabled_models` — Subset of advertised models that the chat UI and AI profile editor expose for selection.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** DeepSeek's current chat models don't accept native image attachments, so every attachment becomes a text stub pointing the model at the workspace tools (`read_workspace_file`, `run_workspace_script`). Text attachments are inlined as `## <name>\n\n<body>`.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### elevenlabs

High-quality text-to-speech via the ElevenLabs API. Used by the core `speaker.announce` flow, the Radio DJ's narration, doorbell greetings, and anything else that calls `TTSBackend.synthesize()`.

**Backend registered** — `TTSBackend.backend_name = "elevenlabs"`.

**Configure** (Settings → TTS, when the `elevenlabs` backend is selected)
- `api_key` *(sensitive)* — ElevenLabs API key.
- `voice_id` — Voice ID to synthesize with (copy from the ElevenLabs voice library).
- `model_id` — ElevenLabs model ID (default `eleven_turbo_v2_5`).
- `cache_max_entries` — LRU cache capacity for recently synthesized phrases (default 256).
- `cache_ttl_seconds` — How long a cached clip lives before re-synthesis (default 1800).

**Config action** — `test_connection`: requests the available voices list to verify the API key.

**No third-party Python dependencies** — talks directly to the REST API via `httpx`.

---

### gemini

Google Gemini chat backend, speaking the [OpenAI-compatible Gemini endpoint](https://ai.google.dev/gemini-api/docs/openai) at `generativelanguage.googleapis.com/v1beta/openai/` directly over `httpx` (no `google-generativeai` SDK). Gemini's pitch is very large context windows (~1M tokens on 2.5 Pro) and native multimodal input.

**Backend registered** — `AIBackend.backend_name = "gemini"`: tool-use capable, streaming, image-input capable on every current model (all 2.5 and 2.0 tiers are natively multimodal), per-call model override.

**Configure** (Settings → Intelligence → AI, with the `gemini` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — Google AI Studio API key (`AIza…`), generated at https://aistudio.google.com/apikey. Distinct from a Google Cloud Vertex AI key — this plugin uses the AI Studio path, not Vertex.
- `base_url` — API base URL (default `https://generativelanguage.googleapis.com/v1beta/openai`).
- `model` — Default model ID (default `gemini-2.5-flash`). Choices: `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.0-flash`, `gemini-1.5-pro`.
- `enabled_models` — Subset exposed to the chat UI and AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Every current Gemini model accepts `image_url` content parts with base64 data URLs on the compat endpoint. Document (PDF) attachments become text stubs pointing the model at the workspace tools — PDFs work on Gemini's native API but the OpenAI-compat layer isn't reliable for them yet.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### google

Bundled Google Workspace integration suite. One plugin, four backends — they share credential plumbing (OAuth, service account, delegated access), so splitting them would just duplicate boilerplate.

**Backends registered**
- `AuthBackend.backend_name = "google"` — OAuth ID token verification for the login system.
- `UserProviderBackend.backend_name = "google_directory"` — syncs Google Workspace users into Gilbert's user store.
- `EmailBackend.backend_name = "gmail"` — used by the Inbox service for polling, threads, drafts, and sending.
- `DocumentBackend.backend_name = "google_drive"` — Google Drive document sync into the Knowledge service.

**Configure**

| Setting | Keys |
|---|---|
| Auth (Google OAuth) | `client_id`, `client_secret` *(sensitive)*, `domain` (optional Workspace domain lock) |
| User provider (Workspace directory) | `sa_json` *(sensitive, service-account JSON)*, `delegated_user`, `domain` |
| Inbox (Gmail) | `service_account_json` *(sensitive)*, `delegated_user`, `email_address` |
| Knowledge (Drive) | `service_account_json` *(sensitive)*, `delegated_user`, `folder_id` |

Each backend exposes a `test_connection` config action that verifies credentials by making a one-off read call.

**Third-party deps**: `google-auth`, `google-api-python-client`.

---

### groq

Groq chat backend — runs open-weight models (Llama, Qwen, Mixtral, DeepSeek distills) on Groq's LPU hardware. Main selling point is inference latency: tokens/second is multiples higher than GPU-hosted providers. Speaks the [OpenAI-compatible endpoint](https://console.groq.com/docs/openai) at `api.groq.com/openai/v1` directly over `httpx`.

**Backend registered** — `AIBackend.backend_name = "groq"`: tool-use capable, streaming, per-call model override.

**Configure** (Settings → Intelligence → AI, with the `groq` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — Groq API key (`gsk_…`).
- `base_url` — API base URL (default `https://api.groq.com/openai/v1`).
- `model` — Default model ID (default `llama-3.3-70b-versatile`).
- `enabled_models` — Subset of advertised models the chat UI and AI profile editor expose. Defaults to the full list: `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `qwen-2.5-32b`, `deepseek-r1-distill-llama-70b`, `gemma2-9b-it`.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Groq's hosted chat models don't accept native image attachments, so every attachment becomes a text stub pointing the model at the workspace tools. Text attachments are inlined as `## <name>\n\n<body>`.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### guess-that-song

Multiplayer music guessing game managed by the AI. The AI picks a track, plays a short clip on the speakers, and players type their guesses in chat. Scoring, round timing, and leaderboards are tracked per-conversation via UI blocks pushed into the chat.

**Service registered** — `guess_game` (requires the `music` and `speaker_control` capabilities — install the `sonos` plugin or another music/speaker backend for this to actually play anything).

**Configure** (Settings → Games → Guess That Song)
- `clip_seconds` — How long each clip plays before guessing opens (default `5.0`).
- `round_time_seconds` — How long players have to guess (default `20.0`).
- `points_correct` — Points awarded per correct guess (default `10`).
- `hint_threshold` — Seconds remaining before a hint drops (default `10.0`).

**No third-party Python dependencies.**

---

### mistral

Mistral AI chat backend, speaking the [OpenAI-compatible La Plateforme API](https://docs.mistral.ai/api/) at `api.mistral.ai/v1` directly over `httpx`. Runs the Mistral Large / Medium / Small lineup plus Codestral and the multimodal Pixtral.

**Backend registered** — `AIBackend.backend_name = "mistral"`: tool-use capable, streaming, image-input capable on Pixtral models, per-call model override.

**Configure** (Settings → Intelligence → AI, with the `mistral` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — Mistral La Plateforme API key.
- `base_url` — API base URL (default `https://api.mistral.ai/v1`).
- `model` — Default model ID (default `mistral-large-latest`). Choices include `mistral-large-latest`, `mistral-medium-latest`, `mistral-small-latest`, `codestral-latest`, `open-mistral-nemo`, `pixtral-large-latest`.
- `enabled_models` — Subset exposed to the chat UI and AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Pixtral models accept `image_url` content parts with base64 data URLs (same shape as OpenAI). Non-vision models receive images as text stubs. Document (PDF) attachments become text stubs pointing the model at the workspace tools.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### ngrok

Tunnel backend that gives Gilbert a public HTTPS URL via [ngrok](https://ngrok.com/) — needed for OAuth callbacks (Google login, Slack Socket Mode) when you're running Gilbert behind NAT without a stable public DNS name.

**Backend registered** — `TunnelBackend.backend_name = "ngrok"`.

**Configure** (Settings → Infrastructure → Tunnel)
- `api_key` *(sensitive)* — ngrok auth token from `dashboard.ngrok.com`.
- `domain` — Optional custom ngrok domain (e.g. `myapp.ngrok.io`). Leave empty to get a random one.

**Config action** — `test_connection`: reports the current public URL if the tunnel is live.

**Third-party deps**: `pyngrok`.

---

### ollama

Local Ollama AI backend — chat against any open-weight model you've `ollama pull`ed, running inference on your own machine. Speaks [Ollama's OpenAI-compatible endpoint](https://github.com/ollama/ollama/blob/main/docs/openai.md) at `http://localhost:11434/v1` directly over `httpx`. No API key required for local usage; proxied/remote instances can set one and it flows through as a Bearer token.

**Backend registered** — `AIBackend.backend_name = "ollama"`: tool-use capable (model-dependent), streaming, image-input capable on multimodal tags (`llava`, `llama3.2-vision`, `qwen2.5-vl`), per-call model override.

**Models.** Whatever the user has pulled locally — `ollama pull llama3.3`, `ollama pull qwen2.5-coder:32b`, etc. The `model` field is free-text because the available set depends on local installs. A curated list of common tool-capable tags ships as suggestions in the `enabled_models` dropdown: `llama3.3`, `llama3.2`, `qwen2.5`, `qwen2.5-coder`, `deepseek-r1`, `mistral`, `mistral-nemo`, `phi4`, `gemma3`.

**Configure** (Settings → Intelligence → AI, with the `ollama` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive, optional)* — Leave blank for local Ollama. Populate only when Ollama sits behind a reverse proxy that gates access.
- `base_url` — Ollama server URL (default `http://localhost:11434/v1`). Point at another host/port if Ollama runs elsewhere on your LAN.
- `model` — Default model tag (default `llama3.3`). Must be a tag you've pulled — Ollama rejects unknown tags.
- `enabled_models` — Suggested subset shown in the chat UI / AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Multimodal Ollama models accept `image_url` content parts with base64 data URLs. Text-only models ignore vision parts, so the same payload is safe regardless of which tag is selected.

**Config action** — `test_connection`: issues a one-word completion to verify the server is reachable and the default model tag is installed.

---

### openai

OpenAI GPT chat backend, speaking the [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) directly over `httpx` (no `openai` SDK dependency). Runs alongside the `anthropic` backend — configure either or both, then pick per-profile in the AI profile editor.

**Backend registered** — `AIBackend.backend_name = "openai"`: tool-use capable, streaming, image-input capable on vision models (`gpt-4o`, `gpt-4-turbo`), per-call model override.

**Configure** (Settings → Intelligence → AI, with the `openai` backend selected)
- `enabled` — Initialize this backend at startup (default `true`). Uncheck to hide its settings and stop it being offered in profile dropdowns.
- `api_key` *(sensitive)* — OpenAI API key (`sk-…`).
- `base_url` — API base URL (default `https://api.openai.com/v1`). Override to point at an OpenAI-compatible proxy (Azure OpenAI, a local gateway, …).
- `organization` — Optional OpenAI organization ID sent as the `OpenAI-Organization` header. Leave blank unless your account belongs to multiple orgs.
- `model` — Default model ID used when a request specifies no per-call model (default `gpt-4o`).
- `enabled_models` — Subset of advertised models that the chat UI and AI profile editor expose for selection. Defaults to every model the backend knows about (`gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `o1`, `o1-mini`, `o3-mini`).
- `max_tokens` — Per-response cap, sent as `max_completion_tokens` so it works for both classic chat models and the `o`-series reasoning models (default `16384`).
- `temperature` — Sampling temperature (default `0.7`). Automatically omitted from requests when the selected model is in the `o`-series, which only accepts the default sampling.

**Streaming.** The backend implements `generate_stream` over OpenAI's SSE chunks, translating `delta.content` into `TEXT_DELTA` events and assembling incremental `tool_calls[i].function.arguments` deltas back into complete `ToolCall`s at the end of the stream. All OpenAI-specific SSE parsing stays inside `openai_ai.py`; `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Image attachments are rendered as `image_url` content parts with `data:<mime>;base64,…` URLs, which the vision-capable models (`gpt-4o`, `gpt-4-turbo`) understand natively. Document (PDF) attachments become text stubs pointing the model at the workspace tools (`read_workspace_file`, `run_workspace_script`) — Chat Completions doesn't accept PDFs directly. Text attachments are inlined as `## <name>\n\n<body>`.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### openrouter

OpenRouter chat backend — a meta-provider that fronts ~200 models from Anthropic, OpenAI, Google, Meta, Mistral, DeepSeek, xAI, Qwen, and more behind a single API key and a unified [OpenAI-compatible endpoint](https://openrouter.ai/docs). Handy for experimenting across providers without signing up with each one, and for routing a single Gilbert install to different frontier models per profile tier.

**Backend registered** — `AIBackend.backend_name = "openrouter"`: tool-use capable, streaming, image-input capable for vision-capable models, per-call model override.

**Model slugs.** Models are addressed as `provider/model`, e.g. `anthropic/claude-sonnet-4-5`, `openai/gpt-4o`, `google/gemini-2.5-pro`, `meta-llama/llama-3.3-70b-instruct`. The plugin ships with a curated list of popular tool-capable slugs; the `model` field is free-text so users can paste any slug from https://openrouter.ai/models without patching the plugin.

**Configure** (Settings → Intelligence → AI, with the `openrouter` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — OpenRouter API key (`sk-or-v1-…`).
- `base_url` — API base URL (default `https://openrouter.ai/api/v1`).
- `site_url` — Optional `HTTP-Referer` URL sent to OpenRouter for attribution on their public leaderboard. Blank = anonymous.
- `site_name` — Optional `X-Title` name sent with the same purpose.
- `model` — Default model slug (default `anthropic/claude-sonnet-4-5`).
- `enabled_models` — Subset of the curated slug list exposed to the chat UI and AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Vision-capable models on OpenRouter (Claude, GPT-4o, Gemini, Pixtral, Grok Vision, …) accept `image_url` content parts with base64 data URLs. Text-only models ignore vision parts, so the same payload is safe regardless of model choice.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### qwen

Alibaba Qwen chat backend, speaking DashScope's [OpenAI-compatible Chat Completions endpoint](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope) directly over `httpx` (no `dashscope` SDK dependency). Because DashScope accepts OpenAI's request shape, streaming protocol, and tool-calling format verbatim, the backend runs alongside `openai` and `anthropic` with the same capabilities — configure one or several, then pick per-profile in the AI profile editor.

**Backend registered** — `AIBackend.backend_name = "qwen"`: tool-use capable, streaming, image-input capable on vision models (`qwen-vl-max`, `qwen-vl-plus`), per-call model override.

**Configure** (Settings → Intelligence → AI, with the `qwen` backend selected)
- `enabled` — Initialize this backend at startup (default `true`). Uncheck to hide its settings and stop it being offered in profile dropdowns.
- `api_key` *(sensitive)* — DashScope API key (`sk-…`).
- `base_url` — API base URL (default `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`). Switch to `https://dashscope.aliyuncs.com/compatible-mode/v1` for the mainland-China endpoint, or point at a local OpenAI-compatible proxy.
- `model` — Default model ID used when a request specifies no per-call model (default `qwen-plus`).
- `enabled_models` — Subset of advertised models that the chat UI and AI profile editor expose for selection. Defaults to every model the backend knows about (`qwen3-max`, `qwen-max`, `qwen-plus`, `qwen-turbo`, `qwen2.5-72b-instruct`, `qwen2.5-32b-instruct`, `qwen2.5-coder-32b-instruct`, `qwq-32b-preview`, `qwen-vl-max`, `qwen-vl-plus`).
- `max_tokens` — Per-response cap (default `8192`). Sent as the standard OpenAI `max_tokens` field — no `o`-series-style `max_completion_tokens` workaround needed.
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** The backend implements `generate_stream` over DashScope's SSE chunks, which use the same wire format as OpenAI — `delta.content` becomes `TEXT_DELTA` events, and incremental `tool_calls[i].function.arguments` deltas are reassembled back into complete `ToolCall`s at the end of the stream. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** Image attachments are rendered as `image_url` content parts with `data:<mime>;base64,…` URLs, which the `qwen-vl-*` models understand natively. Document (PDF) attachments become text stubs pointing the model at the workspace tools (`read_workspace_file`, `run_workspace_script`) — the compatible-mode endpoint doesn't accept PDFs directly. Text attachments are inlined as `## <name>\n\n<body>`.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

### slack

Socket Mode bot that routes Slack DMs and `@Gilbert` mentions to the AI service. Users can chat with Gilbert from Slack with the same tool access, slash commands, and conversation history they have in the web UI. Thread replies where Gilbert is participating are automatically picked up.

**Service registered** — `slack` (requires the `ai_chat` capability, optionally `users` for email-to-user resolution).

**Configure** (Settings → Communication → Slack)
- `bot_token` *(sensitive)* — Slack bot token (`xoxb-…`).
- `app_token` *(sensitive)* — Slack app-level token (`xapp-…`). Required for Socket Mode.
- `ai_profile` — AI profile name routing Slack chat through a specific tier/backend/model (default `standard`).

Slack signing secrets aren't needed — Socket Mode doesn't use HTTP webhooks, so there's nothing for Slack to sign.

**Third-party deps**: `slack-bolt`.

---

### sonos

Sonos speaker control and Sonos-embedded music service discovery. Registers both a speaker backend (playback, volume, grouping, TTS announcements) and a music backend (Spotify via the SMAPI-linked service on the Sonos network).

**Backends registered**
- `SpeakerBackend.backend_name = "sonos"` — handles `play_uri`, volume, grouping, snapshots/restore, now-playing.
- `MusicBackend.backend_name = "sonos"` — browse and search the music services the user has linked via the Sonos app (Spotify, Apple Music, etc.).

**Configure** (Settings → Media → Speakers / Music)
- `preferred_service` — Preferred music service name when multiple are linked (e.g. `"Spotify"`).
- `auth_token` / `auth_key` *(sensitive)* — Credentials for the Sonos-linked music service, captured by the link flow below.

**Config actions**
- `test_connection` — Verifies the linked music service is reachable.
- `link_spotify` / `link_spotify_complete` — Two-phase flow that walks the user through linking Spotify via the Sonos app so Gilbert can play tracks.

**Third-party deps**: `soco` (Sonos controller library).

---

### tavily

Web search backend. Used by the Web Search service's `web_search` and `image_search` tools (slash: `/web search …`, `/web images …`). Tavily's API also returns an AI-generated summary of the top results, which Gilbert surfaces as the first "result."

**Backend registered** — `WebSearchBackend.backend_name = "tavily"`.

**Configure** (Settings → Intelligence → Web Search)
- `api_key` *(sensitive)* — Tavily API key.
- `timeout` — HTTP timeout in seconds (default `15`).

**Config action** — `test_connection`: runs a one-result search to verify the API key.

**No third-party Python dependencies** — talks directly to the REST API via `httpx`.

---

### tesseract

Local OCR backend using [Tesseract](https://tesseract-ocr.github.io/) via `pytesseract`. Runs entirely offline — no network, no API keys. Used by the OCR service for extracting text from images before indexing them in the knowledge base or analyzing them for the vision pipeline.

Requires the Tesseract binary to be installed on the host OS (`apt install tesseract-ocr`, `brew install tesseract`, etc.) — `pytesseract` is just a wrapper.

**Backend registered** — `OCRBackend.backend_name = "tesseract"`.

**Configure** (Settings → Intelligence → OCR)
- `language` — Tesseract language code or pipe-separated list (e.g., `"eng"`, `"eng+fra"`; default `"eng"`).

**Third-party deps**: `pytesseract` (plus the system Tesseract binary).

---

### unifi

Ubiquiti UniFi integration that aggregates signals from multiple UniFi subsystems into a single presence backend, plus a doorbell backend for UniFi Protect camera ring events. Composite design: one plugin registers two distinct backends (`PresenceBackend "unifi"` and `DoorbellBackend "unifi"`), each aggregating whichever UniFi subsystems you have configured.

**Backends registered**
- `PresenceBackend.backend_name = "unifi"` — aggregates UniFi Network WiFi clients, UniFi Protect face detections, and UniFi Access badge events into one presence signal per user.
- `DoorbellBackend.backend_name = "unifi"` — watches UniFi Protect cameras for ring events.

**Configure** (Settings → Monitoring → Presence / Doorbell)

The presence backend has three sub-sections that can each be enabled independently:

| Subsystem | Keys |
|---|---|
| UniFi Network | `unifi_network.host`, `unifi_network.username`, `unifi_network.password` *(sensitive)*, `unifi_network.verify_ssl` |
| UniFi Protect | `unifi_protect.host`, `unifi_protect.username`, `unifi_protect.password` *(sensitive)*, `unifi_protect.verify_ssl` |
| UniFi Access | `unifi_access.host`, `unifi_access.api_token` *(sensitive)*, `unifi_access.verify_ssl` |

The doorbell backend uses a flat config pointing at Protect:
- `host` — UniFi Protect host.
- `username` / `password` *(sensitive)* — Protect credentials.
- `doorbell_names` — Array of camera names to treat as doorbells.

**Config action** — `test_connection`: pings each configured subsystem and reports status.

**No third-party Python dependencies** — all UniFi APIs are spoken via `httpx`/`aiohttp`.

---

### xai

xAI Grok chat backend, speaking the [OpenAI-compatible xAI API](https://docs.x.ai/docs/api-reference) at `api.x.ai/v1` directly over `httpx`. Runs the Grok 4 / Grok 3 / Grok 2 lineup including the `grok-2-vision-1212` multimodal model.

**Backend registered** — `AIBackend.backend_name = "xai"`: tool-use capable, streaming, image-input capable on `grok-2-vision-1212`, per-call model override.

**Configure** (Settings → Intelligence → AI, with the `xai` backend selected)
- `enabled` — Initialize this backend at startup (default `true`).
- `api_key` *(sensitive)* — xAI API key (`xai-…`).
- `base_url` — API base URL (default `https://api.x.ai/v1`).
- `model` — Default model ID (default `grok-4-0709`). Choices: `grok-4-0709`, `grok-3`, `grok-3-mini`, `grok-2-vision-1212`, `grok-2-1212`.
- `enabled_models` — Subset exposed to the chat UI and AI profile editor.
- `max_tokens` — Per-response cap (default `8192`).
- `temperature` — Sampling temperature (default `0.7`).

**Streaming.** OpenAI-compatible SSE — `delta.content` → `TEXT_DELTA`, streamed `tool_calls[i].function.arguments` deltas reassembled into complete `ToolCall`s. `capabilities()` reports `streaming=True, attachments_user=True`.

**Attachments.** `grok-2-vision-1212` accepts `image_url` content parts with base64 data URLs. Non-vision Grok models ignore image parts, so sending the data URL is safe. Document (PDF) attachments become text stubs pointing the model at the workspace tools.

**Config action** — `test_connection`: issues a one-word completion to verify credentials.

---

## Adding a plugin

Every plugin is a standalone directory. The minimum layout:

```
my-plugin/
    plugin.yaml      # manifest (name, version, provides, requires, depends_on)
    plugin.py        # defines create_plugin() → Plugin instance
    pyproject.toml   # declares the plugin's third-party Python deps
    __init__.py      # empty, makes the directory a package for relative imports
    my_backend.py    # the actual integration code — implements a Gilbert ABC
    tests/
        conftest.py  # registers gilbert_plugin_<name> for pytest
        test_my_backend.py
```

### `plugin.yaml`

```yaml
name: my-plugin
version: "1.0.0"
description: "One-line description that shows up in /plugin list"

provides:
  - my_backend_name

requires: []     # Gilbert capabilities this plugin needs (e.g. ["music", "speaker_control"])
depends_on: []   # Other plugins this plugin depends on
```

### `plugin.py`

For a backend-only plugin, `setup()` just imports the module that defines the backend class — the ABC's `__init_subclass__` hook auto-registers it:

```python
from __future__ import annotations
from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta

class MyPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="my-plugin",
            version="1.0.0",
            description="One-liner",
            provides=["my_backend_name"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import my_backend  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass

def create_plugin() -> Plugin:
    return MyPlugin()
```

For a service-registering plugin, create the service instance and call `context.services.register()` — see `slack/plugin.py` or `arr/plugin.py` for examples.

### `pyproject.toml`

Every plugin needs one, even if it has zero third-party deps — Gilbert's `[tool.uv.workspace]` glob expects every workspace member to have a `pyproject.toml`:

```toml
[project]
name = "gilbert-plugin-my-plugin"
version = "1.0.0"
description = "One-liner"
requires-python = ">=3.12"
dependencies = [
    "some-library>=1.2.3",  # drop the list if no third-party deps
]

[tool.uv]
package = false             # virtual workspace member — no wheel is built
```

Gilbert's root `pyproject.toml` adds each plugin as a workspace member under `[tool.uv.sources]` so a plain `uv sync` installs every plugin's deps in one shot.

### `tests/conftest.py`

Pytest needs a little help to treat the plugin directory as the Python package `gilbert_plugin_<name>` so that intra-plugin relative imports work during test collection. Copy `tesseract/tests/conftest.py` as a starting point — it handles the common case of a single-module plugin.

If your plugin has **multiple modules that import each other relatively** (`from .foo import Bar` inside one module), use `unifi/tests/conftest.py` as a template — it has the crucial comment about **not** passing `submodule_search_locations=[]` to `spec_from_file_location`, which would otherwise cause relative imports to resolve to a second copy of the module. The unifi test suite found this the hard way.

### Runtime install flow

A plugin can also be installed at runtime via `/plugin install <github-url>`:

- If the plugin has **no third-party Python deps** (empty `dependencies = []` in its `pyproject.toml`), it hot-loads immediately — no restart needed.
- If it **has deps**, Gilbert persists the install with `needs_restart=True`, returns a message, and waits. Run `/plugin restart` to trigger `gilbert.sh`'s supervisor loop — it re-runs `uv sync` (picking up the new workspace member), then relaunches Gilbert. The boot loader then imports the plugin normally and the restart flag is cleared.

See the main Gilbert `CLAUDE.md` for the full description of the supervisor loop and exit-code convention.

## Running tests

From the Gilbert repo root:

```bash
# Everything
uv run pytest

# A specific plugin
uv run pytest std-plugins/<plugin>/tests/ -v

# Type checking (Gilbert's core + interfaces, which plugins must satisfy)
uv run mypy src/

# Linting (run from gilbert root, --extra dev)
uv run ruff check std-plugins/
```

Gilbert's `pyproject.toml` lists `std-plugins` in `testpaths`, so plugin tests are automatically discovered when you run `uv run pytest` from the Gilbert root.

## Keeping this README accurate

**The table of plugins and every per-plugin section above MUST be updated whenever a plugin is added, removed, renamed, or has its configuration schema change.** This README is the canonical reference for "what plugins exist and how do I configure them" — outdated docs here will mislead users and confuse future Claude sessions. Claude agents working in this repo should treat README drift as a regression and fix it in the same change that modifies a plugin.

## License

MIT
