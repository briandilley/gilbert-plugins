# The model installer is multi-source via a `ModelSource` abstraction; Ollama is a curated list, not a faked search

The model-manager installer browses **multiple sources** through one provider-neutral abstraction
(`model-manager/sources.py`): a `ModelSource` ABC with `descriptor()` + async `search()` + async
`list_variants()`, each source mapping its own catalog onto the source-agnostic `SourceModel` /
`SourceVariant` shapes. The SPA picks a source first, then renders that source's own search/filter
affordances from its `SourceDescriptor`, then finds a model and pulls a variant. Two sources ship:
**Hugging Face** (live GGUF search, `kind=search`) and the **Ollama library** (`kind=curated`).
Adding a third source later is purely additive — register one more `ModelSource` and the service, the
WS RPCs (`model_manager.sources.list` / `.source.search` / `.source.variants`), and the SPA discover
it automatically.

A `SourceVariant` carries the **exact `pull_ref`** the runtime needs (`hf.co/<repo>:<quant>` for HF, a
bare registry tag like `llama3.3:70b` for Ollama) and a `size_bytes` — *exact* for HF (the Hub blob
size), *estimated* for Ollama (params × a Q4_K_M bytes-per-param factor) and flagged
`size_estimated=True` — so the existing hardware-fit policy (`fit.py`) classifies every variant
uniformly across sources without a per-source special case.

## Considered options

- **One source, HF only (status quo before S10)** — rejected: many users think in Ollama-registry
  names (`llama3.3`, `qwen2.5-coder`) and the registry is the canonical "just works" path; restricting
  to HF GGUF repos hides that whole catalog and forces users to find the matching HF repo by hand.
- **Scrape `ollama.com/library` to fake a live Ollama search** — rejected: Ollama exposes **no public
  search/JSON API** for its library. HTML scraping is brittle (markup changes break it silently), can't
  be done honestly as "search", and risks presenting a stale/partial set as authoritative. We instead
  ship a small, hand-vetted curated list and label it **"Curated list"** in the UI, so the limitation is
  surfaced, never disguised.
- **A per-source `if source == "ollama"` branch in the service / SPA** — rejected: it re-introduces the
  source coupling the abstraction exists to remove. Adding a source would mean editing the service, the
  RPC handlers, and the page. The `ModelSource` registry keeps all three source-agnostic.
- **Estimate Ollama tag sizes by probing the daemon / pulling a manifest** — rejected for the browse
  path: it's a network round-trip per tag for a number we only need to *classify* fit, and the daemon
  may not even have the model. The params-based Q4_K_M estimate is cheap, offline, and good enough to
  bucket a tag into fits-VRAM / fits-RAM / won't-fit; it's flagged estimated so an estimate is never
  shown as exact.

## Consequences

The quant-tag **pull pre-flight** (which rejects junk quant schemes like `Q8_K_P` before touching the
runtime, ADR context in the README's "Pull robustness") is now **scoped to Hugging Face refs only** —
a bare Ollama registry tag's suffix (`:70b`) is a *size*, not a quantization scheme, so validating it
against `OLLAMA_PULLABLE_QUANTS` would wrongly reject every Ollama-library pull. `_ws_pull` decides
HF-ness from an `hf.co/` ref prefix or the presence of a `repo_id`.

The Ollama curated list (`ollama_library.py`) is now a second place (alongside `recommended.py` for HF
and the `ollama` backend's `_RECOMMENDED_OVERLAY`) that hand-lists model families; these can drift.
They're deliberately separate because they key on different things — HF repo ids, Ollama registry
names, and Ollama installed tags respectively — but a future consolidation could share one family
list. The size estimate's accuracy is bounded by the Q4_K_M assumption; a model served at a different
default quant will be mis-sized, which only affects the *advisory* fit badge, never the pull itself
(the pull uses the real bare tag and Ollama fetches whatever that tag actually is).

The legacy `model_manager.catalog.search` / `.catalog.quants` RPCs are kept for back-compat; the HF
source delegates to the same `hf_catalog` client, so there is one HF code path, not two.
