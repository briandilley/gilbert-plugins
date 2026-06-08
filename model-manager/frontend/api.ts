/**
 * useModelManagerApi — plugin-local WS RPC bindings for the model manager.
 *
 * Lives inside the model-manager plugin so core's ``useWsApi`` doesn't need
 * to know about manager-specific RPCs. Components inside the plugin call
 * ``const api = useModelManagerApi()`` and get typed bindings for the
 * ``model_manager.*`` frame types implemented by
 * ``ModelManagerService.get_ws_handlers``.
 */

import { useMemo } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import type {
  CatalogQuantsResponse,
  CatalogSearchResponse,
  CatalogSort,
  DeleteResponse,
  HostResourcesResponse,
  InstalledModelsResponse,
  PullResponse,
  SourcesListResponse,
  SourceSearchResponse,
  SourceVariantsResponse,
} from "./types";

// A pull streams a (potentially multi-GB) download that can run for many
// minutes. The server pushes throttled ``model_manager.pull.progress`` events
// carrying this RPC's ``ref`` while it runs, and useWebSocket resets the
// deadline on each — so this large ceiling is really a "no progress at all for
// this long ⇒ treat as dead" backstop, not the expected duration.
const PULL_TIMEOUT_MS = 10 * 60 * 1000;

export function useModelManagerApi() {
  const { rpc } = useWebSocket();

  return useMemo(
    () => ({
      listInstalled: () =>
        rpc<InstalledModelsResponse>({
          type: "model_manager.installed.list",
        }),
      // --- Multi-source installer (S10) ---
      // List the browsable sources (Hugging Face, Ollama library, …). The
      // selector + per-source affordances render from these descriptors.
      listSources: () =>
        rpc<SourcesListResponse>({
          type: "model_manager.sources.list",
        }),
      // Search/list models within a chosen source. A live source hits its
      // remote catalog; a curated source filters its fixed list server-side.
      searchSource: (
        source: string,
        query: string,
        sort: CatalogSort,
        limit: number,
        recommendedOnly = false,
      ) =>
        rpc<SourceSearchResponse>({
          type: "model_manager.source.search",
          source,
          query,
          sort,
          limit,
          recommended_only: recommendedOnly,
        }),
      // List a model's pullable variants (HF quants / Ollama size tags), each
      // with a hardware-fit verdict and the exact pull ref.
      listSourceVariants: (source: string, modelId: string) =>
        rpc<SourceVariantsResponse>({
          type: "model_manager.source.variants",
          source,
          model_id: modelId,
        }),
      searchCatalog: (
        query: string,
        sort: CatalogSort,
        limit: number,
        recommendedOnly = false,
      ) =>
        rpc<CatalogSearchResponse>({
          type: "model_manager.catalog.search",
          query,
          sort,
          limit,
          recommended_only: recommendedOnly,
        }),
      listQuants: (modelId: string) =>
        rpc<CatalogQuantsResponse>({
          type: "model_manager.catalog.quants",
          model_id: modelId,
        }),
      hostResources: () =>
        rpc<HostResourcesResponse>({
          type: "model_manager.host.resources",
        }),
      // Pull a catalog quant. The server builds ``hf.co/<repo>:<quant>``, but
      // we also send the constructed ``ref`` for clarity; ``repo_id`` lets the
      // server seed per-model config (best-effort context window from HF).
      pull: (repoId: string, quant: string) =>
        rpc<PullResponse>(
          {
            type: "model_manager.pull",
            ref: `hf.co/${repoId}:${quant}`,
            repo_id: repoId,
            quant,
          },
          PULL_TIMEOUT_MS,
        ),
      // Source-neutral pull: pass the exact ``pull_ref`` a source's variant
      // carries (``hf.co/<repo>:<quant>`` for HF, a bare registry tag like
      // ``llama3.3:70b`` for Ollama). ``repoId`` is optional and only used by
      // the server to seed HF per-model config; omit it for Ollama. Shares the
      // 10-min keepalive backstop with ``pull``.
      pullRef: (pullRef: string, repoId?: string) =>
        rpc<PullResponse>(
          {
            type: "model_manager.pull",
            ref: pullRef,
            ...(repoId ? { repo_id: repoId } : {}),
          },
          PULL_TIMEOUT_MS,
        ),
      deleteModel: (tag: string) =>
        rpc<DeleteResponse>({
          type: "model_manager.delete",
          tag,
        }),
    }),
    [rpc],
  );
}
