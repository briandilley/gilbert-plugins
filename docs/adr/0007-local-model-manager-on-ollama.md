# Local-model setup builds on Ollama; Hugging Face is the catalog, not a bundled runtime

The local-model manager is a **separate std-plugin** that uses **Ollama as the runtime / server /
quantizer** (driving the existing `ollama` `AIBackend`) instead of bundling its own inference
runtime (llama.cpp, transformers, vLLM). **Hugging Face Hub is the discovery catalog** — its API
supplies relevance signals and per-quantization GGUF file sizes — and models are installed with
`ollama pull hf.co/<repo>:<quant>`, layered with a small Gilbert-curated *recommended overlay* that
protects the tool-calling requirement against HF's long tail. A model is "exposed" by appearing in
the backend's (now **dynamic**, `/api/tags`-backed) `available_models()`, becoming chat-selectable;
binding it to a Tier (`light`/`standard`/`advanced`) so services route to it is a separate, explicit
step.

The manager declares an *enablement dependency* (core ADR-0018) on the `ollama` backend: if Ollama
isn't enabled the manager doesn't start and is shown disabled-with-reason. *Pulling* needs only the
Ollama daemon reachable; *using* a pulled model needs the backend enabled.

## Considered options

- **Bundle a runtime (llama.cpp / transformers / vLLM) and go direct to HF** — rejected: it
  re-implements Ollama's hardest, most hardware-specific parts (GPU-layer offload, quantization fit,
  per-model tool-call templating) and drags heavy, per-hardware OS deps (compilers, CUDA/ROCm libs,
  multi-GB PyTorch) into the submodule. The `kokoro` plugin (single PyTorch dep, default-disabled)
  shows how heavy even one such dep is.
- **Re-use `openai-compatible` for everything** — that plugin already covers users who run their own
  vLLM / LM Studio / llama.cpp endpoint and want raw throughput; the manager instead targets the
  "I just want a good local model running, easily" path that needs discovery + pull + fit, which a
  BYO-endpoint backend doesn't provide.

## Consequences

Choosing Ollama does **not** forfeit Hugging Face — Ollama pulls GGUF straight from HF. What it
forfeits is **safetensors-only** repos and exotic serving; those users are served by
`openai-compatible` + their own runtime. The `ollama` backend's `available_models()` changes from a
static curated list to a dynamic one reflecting actually-installed tags.

The manager never reads the backend's config directly: `pull` / `list` / `delete` (and the resolved
`base_url`) are reached through a `LocalModelRuntimeProvider` capability the `ollama` plugin
implements, so a future local runtime could replace Ollama without touching the manager. This is
distinct from the *Ollama daemon* being reachable, which the manager declares as a
`runtime_dependencies()` check that exercises `GET /api/tags` (ADR-0003), and from the *backend being
enabled*, which is the enablement dependency (core ADR-0018).
