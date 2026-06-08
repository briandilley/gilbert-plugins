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
      deleteModel: (tag: string) =>
        rpc<DeleteResponse>({
          type: "model_manager.delete",
          tag,
        }),
    }),
    [rpc],
  );
}
