/**
 * ModelManagerPage — full-page local-model manager, mounted at /models.
 *
 * Skeleton slice (S5): lists the models currently installed in the local
 * runtime (Ollama) read via ``model_manager.installed.list``. Hugging Face
 * catalog browsing, hardware-fit verdicts, and one-click pull are later
 * slices that build on this shell — the ``model-manager.toolbar`` slot below
 * is the extension point they'll mount their controls into.
 *
 * The route itself is gated on the ``model_manager`` capability in
 * plugin.py, so this page only mounts when the manager is running (its
 * enablement dependency on the Ollama backend satisfied, toggle on).
 */

import { useQuery } from "@tanstack/react-query";
import { useWebSocket } from "@/hooks/useWebSocket";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { PackageIcon, WifiOffIcon, BoxIcon } from "lucide-react";
import { useModelManagerApi } from "./api";
import type { InstalledModel, InstalledModelsResponse } from "./types";

/** Human-readable on-disk size. ``null`` ⇒ "unknown" (the runtime didn't
 *  report a size). Uses binary units (GiB) to match how Ollama reports
 *  model file sizes. */
function humanSize(bytes: number | null): string {
  if (bytes === null || bytes === undefined || bytes <= 0) return "unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = unit === 0 ? value : Math.round(value * 10) / 10;
  return `${rounded} ${units[unit]}`;
}

export function ModelManagerPage() {
  const api = useModelManagerApi();
  const { connected } = useWebSocket();

  const { data, isLoading, isError } = useQuery<InstalledModelsResponse>({
    queryKey: ["model_manager", "installed"],
    queryFn: api.listInstalled,
    enabled: connected,
    staleTime: 30_000,
  });

  const models: InstalledModel[] = data?.models ?? [];

  return (
    <div className="container mx-auto px-4 py-6 max-w-4xl">
      <header className="flex items-center justify-between gap-3 mb-5">
        <div className="flex items-center gap-3 min-w-0">
          <div className="rounded-md bg-foreground/5 p-2">
            <PackageIcon className="h-5 w-5 text-foreground/70" />
          </div>
          <div className="min-w-0">
            <h1 className="text-xl font-semibold leading-tight">Models</h1>
            <p className="text-xs text-muted-foreground leading-tight">
              Local models installed in Ollama. Browsing and pulling new
              models from Hugging Face arrives in a later release.
            </p>
          </div>
        </div>
        {/* Extension point for future slices (HF browse / fit filter /
            pull). Renders nothing until a panel registers against it. */}
        <PluginPanelSlot slot="model-manager.toolbar" />
      </header>

      {!connected ? (
        <ConnectionPlaceholder />
      ) : isLoading ? (
        <LoadingPlaceholder />
      ) : isError ? (
        <ErrorPlaceholder />
      ) : models.length === 0 ? (
        <EmptyPlaceholder />
      ) : (
        <ul className="divide-y rounded-lg border bg-card">
          {models.map((model) => (
            <li
              key={model.tag}
              className="flex items-center justify-between gap-3 px-4 py-3"
            >
              <div className="flex items-center gap-3 min-w-0">
                <BoxIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="font-mono text-sm truncate">{model.tag}</span>
              </div>
              <span className="shrink-0 rounded bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground tabular-nums">
                {humanSize(model.size_bytes)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ConnectionPlaceholder() {
  return (
    <div className="rounded-lg border border-dashed bg-card/50 px-4 py-3 text-sm text-muted-foreground flex items-center gap-2">
      <WifiOffIcon className="h-4 w-4" /> Connecting…
    </div>
  );
}

function LoadingPlaceholder() {
  return (
    <div className="rounded-lg border bg-card px-4 py-3 text-sm text-muted-foreground">
      Loading installed models…
    </div>
  );
}

function ErrorPlaceholder() {
  return (
    <div className="rounded-lg border bg-card px-4 py-3 text-sm text-rose-600">
      Couldn&apos;t load installed models. Check that the Ollama daemon is
      running and reachable.
    </div>
  );
}

function EmptyPlaceholder() {
  return (
    <div className="rounded-lg border border-dashed bg-card/50 px-4 py-8 text-center">
      <BoxIcon className="mx-auto h-8 w-8 text-muted-foreground/60" />
      <p className="mt-3 text-sm font-medium">No models installed</p>
      <p className="mt-1 text-xs text-muted-foreground">
        Pull a model with{" "}
        <code className="rounded bg-muted px-1 py-0.5 font-mono">
          ollama pull &lt;name&gt;
        </code>{" "}
        and it will appear here.
      </p>
    </div>
  );
}
