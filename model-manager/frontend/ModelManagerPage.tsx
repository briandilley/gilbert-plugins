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
 *  - **Hardware fit** (S7) — each quant carries a fit verdict (badge:
 *    fits-VRAM/fast, fits-RAM/slow, won't-fit, unknown), a host-summary line
 *    explains the host, and a "Compatible" toggle filters out quants — and
 *    models whose best loaded quant won't fit — without reordering anything.
 *  - **Pull / delete** (S8) — each catalog quant has a Pull button
 *    (``model_manager.pull``; coarse progress: a spinner while the awaited
 *    pull runs, then a refresh of the Installed list); each installed model
 *    has a Delete button (``model_manager.delete``, confirm-gated). A pulled
 *    model becomes immediately chat-selectable.
 *
 * The route itself is gated on the ``model_manager`` capability in
 * plugin.py, so this page only mounts when the manager is running.
 */

import { useCallback, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "@/hooks/useWebSocket";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  DownloadCloudIcon,
  HeartIcon,
  ZapIcon,
  CpuIcon,
  XCircleIcon,
  HelpCircleIcon,
  MemoryStickIcon,
  ServerIcon,
  Trash2Icon,
  Loader2Icon,
  CheckCircle2Icon,
} from "lucide-react";
import { useModelManagerApi } from "./api";
import type {
  CatalogModel,
  CatalogQuant,
  CatalogQuantsResponse,
  CatalogSearchResponse,
  CatalogSort,
  FitVerdict,
  HostResourcesResponse,
  InstalledModel,
  InstalledModelsResponse,
} from "./types";

/** Query key for the installed-models list — shared so pull/delete mutations
 *  can invalidate it and the Installed section refetches. */
const INSTALLED_QUERY_KEY = ["model_manager", "installed"];

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

// --- Hardware fit ---------------------------------------------------------

/** Best-to-worst ordering of fit verdicts. Mirrors ``_VERDICT_RANK`` in the
 *  backend ``fit.py`` so the UI's model-level aggregation matches the policy. */
const FIT_RANK: Record<FitVerdict, number> = {
  "fits-vram": 0,
  "fits-ram": 1,
  "wont-fit": 2,
  unknown: 3,
};

/** Reduce a model's per-quant verdicts to its single best-case verdict — a
 *  model is as compatible as its best-fitting quant. Mirrors the backend
 *  ``model_best_fit``. Empty (no quants loaded yet) ⇒ "unknown". */
function bestFit(verdicts: FitVerdict[]): FitVerdict {
  let best: FitVerdict = "unknown";
  for (const v of verdicts) {
    if (FIT_RANK[v] < FIT_RANK[best]) best = v;
  }
  return best;
}

const FIT_META: Record<
  FitVerdict,
  {
    label: string;
    title: string;
    Icon: typeof ZapIcon;
    className: string;
  }
> = {
  "fits-vram": {
    label: "Fits VRAM",
    title: "Fits in GPU VRAM — runs fully on GPU (fast).",
    Icon: ZapIcon,
    className:
      "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  },
  "fits-ram": {
    label: "Fits RAM",
    title: "Fits in system RAM — runs on CPU or partial offload (slower).",
    Icon: CpuIcon,
    className:
      "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  },
  "wont-fit": {
    label: "Won't fit",
    title: "Exceeds both VRAM and available RAM — won't load on this host.",
    Icon: XCircleIcon,
    className:
      "border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300",
  },
  unknown: {
    label: "Unknown",
    title:
      "Fit unknown — Ollama runs remotely, or host resources are unavailable.",
    Icon: HelpCircleIcon,
    className: "border-border bg-muted text-muted-foreground",
  },
};

/** Per-quant fit badge: green/fast (VRAM), amber/slow (RAM), red (won't fit),
 *  neutral (unknown). */
function FitBadge({ fit }: { fit: FitVerdict }) {
  const meta = FIT_META[fit] ?? FIT_META.unknown;
  const { Icon } = meta;
  return (
    <span
      title={meta.title}
      className={`inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium ${meta.className}`}
    >
      <Icon className="h-3 w-3" />
      {meta.label}
    </span>
  );
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
    queryKey: INSTALLED_QUERY_KEY,
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
            <InstalledRow key={model.tag} model={model} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** One installed-model row with a Delete action. Deleting confirms first,
 *  then removes the model from Ollama (and the chat picker) and refetches the
 *  installed list. */
function InstalledRow({ model }: { model: InstalledModel }) {
  const api = useModelManagerApi();
  const queryClient = useQueryClient();

  const del = useMutation({
    mutationFn: () => api.deleteModel(model.tag),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: INSTALLED_QUERY_KEY }),
  });

  const onDelete = () => {
    // No global toast/dialog library here; a native confirm is the honest,
    // dependency-free guard against an accidental destructive delete.
    if (window.confirm(`Delete ${model.tag}? This frees its disk space.`)) {
      del.mutate();
    }
  };

  return (
    <li className="flex items-center justify-between gap-3 px-4 py-3">
      <div className="flex items-center gap-3 min-w-0">
        <BoxIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="font-mono text-sm truncate">{model.tag}</span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="rounded bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground tabular-nums">
          {humanSize(model.size_bytes)}
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-muted-foreground hover:text-rose-600"
          onClick={onDelete}
          disabled={del.isPending}
          aria-label={`Delete ${model.tag}`}
          title="Delete this model"
        >
          {del.isPending ? (
            <Loader2Icon className="h-4 w-4 animate-spin" />
          ) : (
            <Trash2Icon className="h-4 w-4" />
          )}
        </Button>
      </div>
      {del.isError && (
        <span className="text-xs text-rose-600">delete failed</span>
      )}
    </li>
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
  const [compatibleOnly, setCompatibleOnly] = useState(false);

  // Best fit verdict per model, reported up by each CatalogRow once its
  // quants load. The "Compatible" filter reads this to hide models whose best
  // loaded quant won't fit. Honest caveat: a model's fit is only known after
  // its row has been expanded (quants are fetched lazily), so list-level
  // filtering can only act on already-loaded models — never-expanded models
  // stay visible. Per-quant filtering inside an expanded row is exact.
  const [modelFit, setModelFit] = useState<Record<string, FitVerdict>>({});
  const reportFit = useCallback((modelId: string, fit: FitVerdict) => {
    setModelFit((prev) => (prev[modelId] === fit ? prev : { ...prev, [modelId]: fit }));
  }, []);

  const { data, isFetching, isError } = useQuery<CatalogSearchResponse>({
    queryKey: ["model_manager", "catalog", submittedQuery, sort],
    queryFn: () => api.searchCatalog(submittedQuery, sort, 25),
    enabled: connected,
    staleTime: 60_000,
  });

  const all: CatalogModel[] = data?.models ?? [];
  const models = all.filter((m) => {
    if (recommendedOnly && !m.recommended) return false;
    // Compatible filter: a FILTER, not a sort weight. Hide a model only once
    // we *know* its best loaded quant won't fit; unknown / not-yet-loaded
    // models stay visible so the user can still expand them.
    if (compatibleOnly && modelFit[m.id] === "wont-fit") return false;
    return true;
  });

  return (
    <section>
      <h2 className="text-sm font-semibold text-foreground/80 mb-2">
        Browse Hugging Face
      </h2>

      <HostSummary hostResources={api.hostResources} />

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
            id="compatible-only"
            checked={compatibleOnly}
            onCheckedChange={setCompatibleOnly}
          />
          <Label
            htmlFor="compatible-only"
            className="text-xs text-muted-foreground whitespace-nowrap"
            title="Hide quantizations (and models) that won't run on this host."
          >
            Compatible
          </Label>
        </div>

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
            <CatalogRow
              key={model.id}
              model={model}
              listQuants={api.listQuants}
              compatibleOnly={compatibleOnly}
              onFitResolved={reportFit}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

/** A one-line host summary above the Browse list: RAM + GPU/VRAM, or a
 *  "remote Ollama — fit unknown" note. Explains why fit verdicts read the way
 *  they do. Silently renders nothing while loading or on error — it's an
 *  affordance, not load-bearing. */
function HostSummary({
  hostResources,
}: {
  hostResources: () => Promise<HostResourcesResponse>;
}) {
  const { connected } = useWebSocket();
  const { data } = useQuery<HostResourcesResponse>({
    queryKey: ["model_manager", "host", "resources"],
    queryFn: hostResources,
    enabled: connected,
    staleTime: 60_000,
  });

  if (!data) return null;

  if (data.remote) {
    return (
      <p className="mb-3 flex items-center gap-1.5 text-xs text-muted-foreground">
        <ServerIcon className="h-3.5 w-3.5" />
        Remote Ollama — hardware fit is unknown (only the local host can be
        probed).
      </p>
    );
  }

  const ram =
    data.available_ram_bytes !== null && data.total_ram_bytes !== null
      ? `${humanSize(data.available_ram_bytes)} free of ${humanSize(data.total_ram_bytes)} RAM`
      : null;
  const gpu =
    data.gpus.length > 0
      ? data.gpus
          .map((g) =>
            g.total_vram_bytes !== null
              ? `${g.name} (${humanSize(g.total_vram_bytes)} VRAM)`
              : `${g.name} (VRAM unknown)`,
          )
          .join(", ")
      : "no GPU detected";

  if (ram === null) {
    return (
      <p className="mb-3 flex items-center gap-1.5 text-xs text-muted-foreground">
        <HelpCircleIcon className="h-3.5 w-3.5" />
        Host resources unavailable — hardware fit is unknown.
      </p>
    );
  }

  return (
    <p className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <MemoryStickIcon className="h-3.5 w-3.5" />
        {ram}
      </span>
      <span className="flex items-center gap-1.5">
        <CpuIcon className="h-3.5 w-3.5" />
        {gpu}
      </span>
    </p>
  );
}

function CatalogRow({
  model,
  listQuants,
  compatibleOnly,
  onFitResolved,
}: {
  model: CatalogModel;
  listQuants: (modelId: string) => Promise<CatalogQuantsResponse>;
  compatibleOnly: boolean;
  onFitResolved: (modelId: string, fit: FitVerdict) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const { data, isFetching, isError } = useQuery<CatalogQuantsResponse>({
    queryKey: ["model_manager", "catalog", "quants", model.id],
    queryFn: () => listQuants(model.id),
    enabled: expanded,
    staleTime: 5 * 60_000,
  });

  const quants: CatalogQuant[] = data?.quants ?? [];

  // Report this model's best-case fit up to BrowseSection so the Compatible
  // filter can act on it. Runs once quants are loaded for the row.
  useEffect(() => {
    if (data) onFitResolved(model.id, bestFit(quants.map((q) => q.fit)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  // When Compatible is on, drop the quants that won't fit — but keep unknown
  // ones (we can't honestly rule them out). Filtering happens inside the
  // expanded row; list-level filtering is in BrowseSection.
  const shownQuants = compatibleOnly
    ? quants.filter((q) => q.fit !== "wont-fit")
    : quants;

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
          ) : shownQuants.length === 0 ? (
            <p className="px-2 py-1 text-xs text-muted-foreground">
              No compatible quantizations — turn off “Compatible” to see all{" "}
              {quants.length}.
            </p>
          ) : (
            <ul className="divide-y divide-border/60">
              {shownQuants.map((q) => (
                <li
                  key={q.filename}
                  className="flex items-center justify-between gap-3 px-2 py-1.5"
                >
                  <span className="font-mono text-xs truncate">
                    {q.quant_label ?? q.filename}
                  </span>
                  <div className="flex shrink-0 items-center gap-2">
                    <FitBadge fit={q.fit} />
                    <span className="text-xs text-muted-foreground tabular-nums">
                      {humanSize(q.size_bytes)}
                    </span>
                    <PullButton repoId={model.id} quant={q} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

/** Per-quant Pull button.
 *
 * Pulls ``hf.co/<repoId>:<quant_label>`` into Ollama. Progress is coarse by
 * design (gilbert#40): the RPC is awaited to completion, so this shows a
 * "Pulling…" spinner while in flight and a brief "Installed" tick on success —
 * a byte-level progress bar is intentionally out of scope (faking one would be
 * dishonest). On success the installed-models query is invalidated so the
 * Installed section refetches and the model appears there (and in chat).
 *
 * Ollama's ``hf.co/<repo>:<quant>`` ref needs a quant label, so the button is
 * disabled for a gguf whose filename has no recognizable quant marker. */
function PullButton({ repoId, quant }: { repoId: string; quant: CatalogQuant }) {
  const api = useModelManagerApi();
  const queryClient = useQueryClient();

  const pull = useMutation({
    mutationFn: () => api.pull(repoId, quant.quant_label as string),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: INSTALLED_QUERY_KEY }),
  });

  if (!quant.quant_label) {
    return (
      <Button
        variant="outline"
        size="sm"
        className="h-6 px-2 text-[11px]"
        disabled
        title="No quantization label — can't build an Ollama pull reference."
      >
        <DownloadCloudIcon className="h-3 w-3" />
      </Button>
    );
  }

  if (pull.isSuccess) {
    return (
      <span
        className="inline-flex items-center gap-1 text-[11px] text-emerald-600"
        title="Installed — now selectable in chat."
      >
        <CheckCircle2Icon className="h-3.5 w-3.5" />
        Installed
      </span>
    );
  }

  return (
    <Button
      variant="outline"
      size="sm"
      className="h-6 gap-1 px-2 text-[11px]"
      onClick={() => pull.mutate()}
      disabled={pull.isPending}
      title={
        pull.isError
          ? "Pull failed — click to retry."
          : `Pull ${quant.quant_label} into Ollama`
      }
    >
      {pull.isPending ? (
        <>
          <Loader2Icon className="h-3 w-3 animate-spin" />
          Pulling…
        </>
      ) : pull.isError ? (
        <>
          <DownloadCloudIcon className="h-3 w-3 text-rose-600" />
          Retry
        </>
      ) : (
        <>
          <DownloadCloudIcon className="h-3 w-3" />
          Pull
        </>
      )}
    </Button>
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
