/**
 * ModelManagerPage — full-page local-model manager, mounted at /models.
 *
 * Two sections:
 *  - **Installed** — the models currently installed in the local runtime
 *    (Ollama), read via ``model_manager.installed.list``.
 *  - **Browse** (S6) — search the Hugging Face Hub for GGUF models with
 *    HF-native sort, a "Recommended only" client-side filter, and a per-row
 *    expand that lists each quantization with its human-readable size
 *    (``model_manager.catalog.search`` / ``model_manager.catalog.quants``).
 *
 * The hardware "Compatible" fit filter and one-click pull are later slices;
 * Browse shows all models by default.
 *
 * The route itself is gated on the ``model_manager`` capability in
 * plugin.py, so this page only mounts when the manager is running.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useWebSocket } from "@/hooks/useWebSocket";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  PackageIcon,
  WifiOffIcon,
  BoxIcon,
  SearchIcon,
  StarIcon,
  ChevronRightIcon,
  ChevronDownIcon,
  DownloadIcon,
  HeartIcon,
} from "lucide-react";
import { useModelManagerApi } from "./api";
import type {
  CatalogModel,
  CatalogQuant,
  CatalogQuantsResponse,
  CatalogSearchResponse,
  CatalogSort,
  InstalledModel,
  InstalledModelsResponse,
} from "./types";

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

/** Compact thousands formatting for download/like counts. */
function compactCount(n: number): string {
  if (n >= 1_000_000) return `${Math.round(n / 100_000) / 10}M`;
  if (n >= 1_000) return `${Math.round(n / 100) / 10}K`;
  return `${n}`;
}

const SORT_OPTIONS: { value: CatalogSort; label: string }[] = [
  { value: "downloads", label: "Downloads" },
  { value: "likes", label: "Likes" },
  { value: "trending", label: "Trending" },
  { value: "recent", label: "Recent" },
];

export function ModelManagerPage() {
  const { connected } = useWebSocket();

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
              Local models installed in Ollama, and the Hugging Face GGUF
              catalog you can browse to find more.
            </p>
          </div>
        </div>
        {/* Extension point for future slices (fit filter / pull). Renders
            nothing until a panel registers against it. */}
        <PluginPanelSlot slot="model-manager.toolbar" />
      </header>

      {!connected ? (
        <ConnectionPlaceholder />
      ) : (
        <div className="space-y-8">
          <InstalledSection />
          <BrowseSection />
        </div>
      )}
    </div>
  );
}

// --- Installed section ----------------------------------------------------

function InstalledSection() {
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
    <section>
      <h2 className="text-sm font-semibold text-foreground/80 mb-2">
        Installed
      </h2>
      {isLoading ? (
        <LoadingPlaceholder label="Loading installed models…" />
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
    </section>
  );
}

// --- Browse section -------------------------------------------------------

function BrowseSection() {
  const api = useModelManagerApi();
  const { connected } = useWebSocket();

  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [sort, setSort] = useState<CatalogSort>("downloads");
  const [recommendedOnly, setRecommendedOnly] = useState(false);

  const { data, isFetching, isError } = useQuery<CatalogSearchResponse>({
    queryKey: ["model_manager", "catalog", submittedQuery, sort],
    queryFn: () => api.searchCatalog(submittedQuery, sort, 25),
    enabled: connected,
    staleTime: 60_000,
  });

  const all: CatalogModel[] = data?.models ?? [];
  const models = recommendedOnly ? all.filter((m) => m.recommended) : all;

  return (
    <section>
      <h2 className="text-sm font-semibold text-foreground/80 mb-2">
        Browse Hugging Face
      </h2>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <form
          className="relative flex-1 min-w-[12rem]"
          onSubmit={(e) => {
            e.preventDefault();
            setSubmittedQuery(query.trim());
          }}
        >
          <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search GGUF models (e.g. qwen, llama)…"
            className="pl-8"
            aria-label="Search Hugging Face GGUF models"
          />
        </form>

        <Select value={sort} onValueChange={(v) => setSort(v as CatalogSort)}>
          <SelectTrigger className="w-[9.5rem]" aria-label="Sort by">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SORT_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="flex items-center gap-2">
          <Switch
            id="recommended-only"
            checked={recommendedOnly}
            onCheckedChange={setRecommendedOnly}
          />
          <Label
            htmlFor="recommended-only"
            className="text-xs text-muted-foreground whitespace-nowrap"
          >
            Recommended only
          </Label>
        </div>
      </div>

      {isError ? (
        <CatalogErrorPlaceholder />
      ) : isFetching && all.length === 0 ? (
        <LoadingPlaceholder label="Searching Hugging Face…" />
      ) : models.length === 0 ? (
        <CatalogEmptyPlaceholder recommendedOnly={recommendedOnly} />
      ) : (
        <ul className="divide-y rounded-lg border bg-card">
          {models.map((model) => (
            <CatalogRow key={model.id} model={model} listQuants={api.listQuants} />
          ))}
        </ul>
      )}
    </section>
  );
}

function CatalogRow({
  model,
  listQuants,
}: {
  model: CatalogModel;
  listQuants: (modelId: string) => Promise<CatalogQuantsResponse>;
}) {
  const [expanded, setExpanded] = useState(false);

  const { data, isFetching, isError } = useQuery<CatalogQuantsResponse>({
    queryKey: ["model_manager", "catalog", "quants", model.id],
    queryFn: () => listQuants(model.id),
    enabled: expanded,
    staleTime: 5 * 60_000,
  });

  const quants: CatalogQuant[] = data?.quants ?? [];

  return (
    <li className="px-4 py-3">
      <button
        type="button"
        className="flex w-full items-center justify-between gap-3 text-left"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <div className="flex items-center gap-2 min-w-0">
          {expanded ? (
            <ChevronDownIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRightIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
          )}
          <span className="font-mono text-sm truncate">{model.id}</span>
          {model.recommended && (
            <Badge variant="success" className="shrink-0 gap-1">
              <StarIcon className="h-3 w-3" />
              Recommended
            </Badge>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-3 text-xs text-muted-foreground tabular-nums">
          <span className="flex items-center gap-1" title="Downloads">
            <DownloadIcon className="h-3 w-3" />
            {compactCount(model.downloads)}
          </span>
          <span className="flex items-center gap-1" title="Likes">
            <HeartIcon className="h-3 w-3" />
            {compactCount(model.likes)}
          </span>
        </div>
      </button>

      {expanded && (
        <div className="mt-3 ml-6 rounded-md border bg-muted/30 p-2">
          {isError ? (
            <p className="px-2 py-1 text-xs text-rose-600">
              Couldn&apos;t load quantizations for this model.
            </p>
          ) : isFetching ? (
            <p className="px-2 py-1 text-xs text-muted-foreground">
              Loading quantizations…
            </p>
          ) : quants.length === 0 ? (
            <p className="px-2 py-1 text-xs text-muted-foreground">
              No GGUF files found in this repo.
            </p>
          ) : (
            <ul className="divide-y divide-border/60">
              {quants.map((q) => (
                <li
                  key={q.filename}
                  className="flex items-center justify-between gap-3 px-2 py-1.5"
                >
                  <span className="font-mono text-xs truncate">
                    {q.quant_label ?? q.filename}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                    {humanSize(q.size_bytes)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

// --- Placeholders ---------------------------------------------------------

function ConnectionPlaceholder() {
  return (
    <div className="rounded-lg border border-dashed bg-card/50 px-4 py-3 text-sm text-muted-foreground flex items-center gap-2">
      <WifiOffIcon className="h-4 w-4" /> Connecting…
    </div>
  );
}

function LoadingPlaceholder({ label }: { label: string }) {
  return (
    <div className="rounded-lg border bg-card px-4 py-3 text-sm text-muted-foreground">
      {label}
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

function CatalogErrorPlaceholder() {
  return (
    <div className="rounded-lg border bg-card px-4 py-3 text-sm text-rose-600">
      Couldn&apos;t reach the Hugging Face Hub. Check your connection and try
      again.
    </div>
  );
}

function EmptyPlaceholder() {
  return (
    <div className="rounded-lg border border-dashed bg-card/50 px-4 py-8 text-center">
      <BoxIcon className="mx-auto h-8 w-8 text-muted-foreground/60" />
      <p className="mt-3 text-sm font-medium">No models installed</p>
      <p className="mt-1 text-xs text-muted-foreground">
        Browse the catalog below, or pull a model with{" "}
        <code className="rounded bg-muted px-1 py-0.5 font-mono">
          ollama pull &lt;name&gt;
        </code>{" "}
        and it will appear here.
      </p>
    </div>
  );
}

function CatalogEmptyPlaceholder({ recommendedOnly }: { recommendedOnly: boolean }) {
  return (
    <div className="rounded-lg border border-dashed bg-card/50 px-4 py-8 text-center">
      <SearchIcon className="mx-auto h-8 w-8 text-muted-foreground/60" />
      <p className="mt-3 text-sm font-medium">No models found</p>
      <p className="mt-1 text-xs text-muted-foreground">
        {recommendedOnly
          ? "No recommended models match — turn off “Recommended only” to see all results."
          : "Try a different search term or sort order."}
      </p>
    </div>
  );
}
