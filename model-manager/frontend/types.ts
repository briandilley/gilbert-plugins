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
