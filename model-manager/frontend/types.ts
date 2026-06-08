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

/** UI sort keys for the catalog Browse list. Mapped to Hugging Face's own
 *  ``sort`` fields server-side (``downloads``/``likes``/``trendingScore``/
 *  ``lastModified``). */
export type CatalogSort = "downloads" | "likes" | "trending" | "recent";

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
