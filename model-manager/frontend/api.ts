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
  HostResourcesResponse,
  InstalledModelsResponse,
} from "./types";

export function useModelManagerApi() {
  const { rpc } = useWebSocket();

  return useMemo(
    () => ({
      listInstalled: () =>
        rpc<InstalledModelsResponse>({
          type: "model_manager.installed.list",
        }),
      searchCatalog: (query: string, sort: CatalogSort, limit: number) =>
        rpc<CatalogSearchResponse>({
          type: "model_manager.catalog.search",
          query,
          sort,
          limit,
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
    }),
    [rpc],
  );
}
