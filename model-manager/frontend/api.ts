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
import type { InstalledModelsResponse } from "./types";

export function useModelManagerApi() {
  const { rpc } = useWebSocket();

  return useMemo(
    () => ({
      listInstalled: () =>
        rpc<InstalledModelsResponse>({
          type: "model_manager.installed.list",
        }),
    }),
    [rpc],
  );
}
