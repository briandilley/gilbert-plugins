/**
 * Model-manager plugin types.
 */

/** One model tag installed in the local runtime (Ollama). Mirrors the
 *  backend ``InstalledModel`` dataclass serialized by
 *  ``model_manager.installed.list``. */
export interface InstalledModel {
  /** Runtime-local model reference, e.g. ``"llama3.3"``. */
  tag: string;
  /** On-disk size in bytes, or ``null`` when the runtime doesn't report it. */
  size_bytes: number | null;
}

export interface InstalledModelsResponse {
  models: InstalledModel[];
}

// --- Hugging Face catalog browse (S6) ------------------------------------

/** UI sort keys for the catalog Browse list.
 *
 *  The first four map to Hugging Face's own ``sort`` fields server-side
 *  (``downloads``/``likes``/``trendingScore``/``lastModified``). ``powerful``
 *  and ``smallest`` are **derived** — HF can't sort by parameter count, so the
 *  server fetches a larger page by downloads and re-ranks it by ``params_b``
 *  (the top matches, not all of HF). */
export type CatalogSort =
  | "downloads"
  | "likes"
  | "trending"
  | "recent"
  | "powerful"
  | "smallest";

/** One GGUF repo from the Hugging Face Hub catalog. Mirrors the backend
 *  ``CatalogModel`` serialized by ``model_manager.catalog.search``. */
export interface CatalogModel {
  /** Repo id, ``<owner>/<name>``. */
  id: string;
  /** HF download count (0 when absent). */
  downloads: number;
  /** HF like count (0 when absent). */
  likes: number;
  /** ISO-8601 last-modified timestamp, or ``null``. */
  last_modified: string | null;
  /** True iff the repo is in Gilbert's recommended overlay. */
  recommended: boolean;
  /** Approximate parameter count in billions parsed from the id, or ``null``
   *  when no recognizable token. Drives the param chip + size-class filter. */
  params_b: number | null;
}

export interface CatalogSearchResponse {
  models: CatalogModel[];
}

/** Hardware-fit verdict for a quant on the host where Ollama runs. Stable
 *  wire values from the manager's fit policy (``fit.py``):
 *  - ``"fits-vram"`` — fully on GPU (fast).
 *  - ``"fits-ram"``  — CPU / partial offload (slow).
 *  - ``"wont-fit"``  — exceeds both VRAM and RAM.
 *  - ``"unknown"``   — remote Ollama, or no host-resources data. */
export type FitVerdict = "fits-vram" | "fits-ram" | "wont-fit" | "unknown";

/** One ``*.gguf`` quantization of a repo. Mirrors the backend ``Quant``
 *  serialized by ``model_manager.catalog.quants`` (with the S7 ``fit``
 *  field attached). */
export interface CatalogQuant {
  /** File path within the repo. */
  filename: string;
  /** Parsed quant tag (``Q4_K_M``, ``Q8_0``, ``F16``…), or ``null``. */
  quant_label: string | null;
  /** File size in bytes, or ``null`` when HF doesn't report it. */
  size_bytes: number | null;
  /** Hardware-fit verdict on the host where Ollama runs. */
  fit: FitVerdict;
  /** True iff Ollama accepts this quant tag as a pull reference. A junk /
   *  community tag (e.g. ``Q8_K_P``) is not pullable, so the Pull button is
   *  disabled with an explanatory tooltip. */
  pullable: boolean;
}

export interface CatalogQuantsResponse {
  model_id: string;
  quants: CatalogQuant[];
}

// --- Host resources (S7) -------------------------------------------------

/** One GPU on the host. Mirrors the backend ``GPUInfo``. */
export interface HostGpu {
  /** GPU name (e.g. ``"NVIDIA GeForce RTX 4090"``). */
  name: string;
  /** Total VRAM in bytes, or ``null`` when it couldn't be determined. */
  total_vram_bytes: number | null;
}

/** Host summary for the fit line + "unknown" explainer. Mirrors the frame
 *  serialized by ``model_manager.host.resources``. */
export interface HostResourcesResponse {
  /** Total system RAM in bytes, or ``null`` when host data is unavailable. */
  total_ram_bytes: number | null;
  /** Available system RAM in bytes, or ``null`` when unavailable. */
  available_ram_bytes: number | null;
  /** Detected GPUs (empty when none detected or host data unavailable). */
  gpus: HostGpu[];
  /** True when Ollama's ``base_url`` is off-box ⇒ every fit is "unknown". */
  remote: boolean;
}

// --- Multi-source installer (S10) ----------------------------------------

/** How a source is browsed. ``search`` ⇒ a live remote query (Hugging Face);
 *  ``curated`` ⇒ a fixed, hand-vetted list filtered client-side (Ollama
 *  library — no public search API exists). */
export type SourceKind = "search" | "curated";

/** One browsable model source. Mirrors the backend ``SourceDescriptor``
 *  serialized by ``model_manager.sources.list``. The selector + per-source
 *  affordances render from this, so adding a source is purely additive. */
export interface SourceDescriptor {
  /** Stable source id, sent back on every search/variants RPC. */
  id: string;
  /** Friendly selector label. */
  label: string;
  /** One-line blurb for the selector. */
  description: string;
  /** Live ``search`` vs ``curated`` list — drives honest UI labeling. */
  kind: SourceKind;
  /** Placeholder for the search/filter input. */
  search_placeholder: string;
  /** Singular noun for an expanded variant ("quantization" / "size"). */
  variant_noun: string;
  /** Whether the sort dropdown applies (HF yes, curated Ollama no). */
  supports_sort: boolean;
  /** Whether a "Recommended only" toggle is meaningful (HF only). */
  supports_recommended_only: boolean;
}

export interface SourcesListResponse {
  sources: SourceDescriptor[];
}

/** One model row from a source. Mirrors the backend ``SourceModel``
 *  serialized by ``model_manager.source.search``. Generalizes ``CatalogModel``
 *  across sources (``name`` is non-null for the curated Ollama list). */
export interface SourceModel {
  /** Model id within the source (HF repo id, or Ollama registry name). */
  id: string;
  /** Friendly display name, or ``null`` ⇒ fall back to ``id``. */
  name: string | null;
  downloads: number;
  likes: number;
  last_modified: string | null;
  recommended: boolean;
  params_b: number | null;
}

export interface SourceSearchResponse {
  source: string;
  models: SourceModel[];
}

/** One pullable variant of a model — an HF quant or an Ollama size tag.
 *  Mirrors the backend ``SourceVariant`` (with the ``fit`` verdict attached)
 *  serialized by ``model_manager.source.variants``. */
export interface SourceVariant {
  /** Stable per-row key (HF gguf filename, or Ollama registry tag). */
  variant_id: string;
  /** What the UI shows (quant label / size tag). */
  label: string;
  /** Exact reference handed to ``model_manager.pull``. */
  pull_ref: string;
  /** On-disk size — exact for HF, estimated for Ollama, or ``null``. */
  size_bytes: number | null;
  /** True when ``size_bytes`` is an estimate (Ollama), so the UI marks the
   *  fit verdict approximate rather than exact. */
  size_estimated: boolean;
  /** Hardware-fit verdict on the host where Ollama runs. */
  fit: FitVerdict;
  /** Whether the runtime accepts ``pull_ref`` as a pull reference. */
  pullable: boolean;
  /** Parameter count this variant pins (Ollama size tag), or ``null``. */
  params_b: number | null;
}

export interface SourceVariantsResponse {
  source: string;
  model_id: string;
  variants: SourceVariant[];
}

// --- Pull / delete (S8) ---------------------------------------------------

/** Result of ``model_manager.pull``. The RPC returns only once the pull has
 *  completed (coarse progress: a spinner while in flight, then this). Mirrors
 *  the frame serialized by ``ModelManagerService._ws_pull``. */
export interface PullResponse {
  /** The installed model tag Ollama now serves chat under
   *  (``hf.co/<repo>:<quant>``). */
  model: string;
  /** True when per-model config was seeded (the ``ai_model_config``
   *  capability was present). */
  seeded: boolean;
  /** Best-effort context window read from HF metadata, or ``null``. */
  context_window: number | null;
}

/** Result of ``model_manager.delete``. */
export interface DeleteResponse {
  /** The tag that was removed. */
  tag: string;
}
