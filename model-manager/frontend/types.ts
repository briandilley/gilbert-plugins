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

/** One ``*.gguf`` quantization of a repo. Mirrors the backend ``Quant``
 *  serialized by ``model_manager.catalog.quants``. */
export interface CatalogQuant {
  /** File path within the repo. */
  filename: string;
  /** Parsed quant tag (``Q4_K_M``, ``Q8_0``, ``F16``…), or ``null``. */
  quant_label: string | null;
  /** File size in bytes, or ``null`` when HF doesn't report it. */
  size_bytes: number | null;
}

export interface CatalogQuantsResponse {
  model_id: string;
  quants: CatalogQuant[];
}
