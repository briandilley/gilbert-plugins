/**
 * ModelManagerPage — full-page local-model manager, mounted at /models.
 *
 * Two sections:
 *  - **Installed** — the models currently installed in the local runtime
 *    (Ollama), read via ``model_manager.installed.list``.
 *  - **Browse** (S6) — search the Hugging Face Hub for GGUF models with
 *    HF-native sort plus derived "Most powerful" / "Smallest" param re-ranks,
 *    a server-sourced "Recommended only" overlay, a size-class quick filter,
 *    a param chip per row, and a per-row expand that lists each quantization
 *    with its human-readable size
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
  LayersIcon,
} from "lucide-react";
import { useModelManagerApi } from "./api";
import type {
  CatalogSort,
  FitVerdict,
  HostResourcesResponse,
  InstalledModel,
  InstalledModelsResponse,
  SourceDescriptor,
  SourceModel,
  SourcesListResponse,
  SourceSearchResponse,
  SourceVariant,
  SourceVariantsResponse,
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
  // Derived re-ranks (not true HF sorts) — see the dropdown's note below.
  { value: "powerful", label: "Most powerful" },
  { value: "smallest", label: "Smallest" },
];

/** A "derived" sort re-ranks the top matches by parameter count rather than
 *  being a true Hub sort (HF can't sort by params). Used to surface an honest
 *  caveat under the sort dropdown. */
function isDerivedSort(sort: CatalogSort): boolean {
  return sort === "powerful" || sort === "smallest";
}

// --- Size-class filter ----------------------------------------------------

/** A parameter-size bucket the user can toggle (client-side over
 *  ``params_b``). Composes with the Compatible filter (AND). */
type SizeClass = "tiny" | "small" | "medium" | "large";

const SIZE_CLASSES: { value: SizeClass; label: string; title: string }[] = [
  { value: "tiny", label: "≤4B", title: "4 billion parameters or fewer" },
  { value: "small", label: "7–9B", title: "7 to 9 billion parameters" },
  { value: "medium", label: "13–34B", title: "13 to 34 billion parameters" },
  { value: "large", label: "70B+", title: "70 billion parameters or more" },
];

/** Which size bucket a parameter count (in billions) falls into, or ``null``
 *  when unknown (no recognizable token) — unknowns never match a bucket, so a
 *  model with unknown params is hidden once any bucket is selected. */
function sizeClassOf(paramsB: number | null): SizeClass | null {
  if (paramsB === null || paramsB === undefined) return null;
  if (paramsB <= 4) return "tiny";
  if (paramsB < 13) return "small"; // 7–9B band (and anything 4<p<13)
  if (paramsB < 70) return "medium"; // 13–34B band (and anything 13<=p<70)
  return "large";
}

/** Human param chip text: ``~32B`` / ``~3.8B`` / ``~500M`` / ``~?``. */
function paramChip(paramsB: number | null): string {
  if (paramsB === null || paramsB === undefined) return "~?";
  if (paramsB < 1) return `~${Math.round(paramsB * 1000)}M`;
  // Whole numbers render without a decimal; ``3.8`` keeps one.
  const v = Number.isInteger(paramsB) ? paramsB : Math.round(paramsB * 10) / 10;
  return `~${v}B`;
}

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

// --- Browse section (multi-source, S10) -----------------------------------

/** The Browse section: pick a SOURCE first, then that source's own search /
 *  filter affordances appear, then find a model and pull a variant. The source
 *  list drives everything, so adding a source is purely additive (no edits
 *  here). */
function BrowseSection() {
  const api = useModelManagerApi();
  const { connected } = useWebSocket();

  const { data: sourcesData, isLoading: sourcesLoading } =
    useQuery<SourcesListResponse>({
      queryKey: ["model_manager", "sources"],
      queryFn: api.listSources,
      enabled: connected,
      staleTime: 5 * 60_000,
    });

  const sources = sourcesData?.sources ?? [];
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Default to the first source once they load (Hugging Face today).
  useEffect(() => {
    if (selectedId === null && sources.length > 0) setSelectedId(sources[0].id);
  }, [selectedId, sources]);

  const selected = sources.find((s) => s.id === selectedId) ?? null;

  return (
    <section>
      <h2 className="text-sm font-semibold text-foreground/80 mb-2">
        Browse &amp; install
      </h2>

      <HostSummary hostResources={api.hostResources} />

      {sourcesLoading ? (
        <LoadingPlaceholder label="Loading sources…" />
      ) : sources.length === 0 ? (
        <LoadingPlaceholder label="No model sources available." />
      ) : (
        <>
          <SourceSelector
            sources={sources}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
          {selected && <SourceBrowser key={selected.id} source={selected} />}
        </>
      )}
    </section>
  );
}

/** Step 1 — the source selector. A row of cards; the active one is highlighted.
 *  Curated sources carry a small badge so it's clear they're a hand-picked list
 *  rather than a full search. */
function SourceSelector({
  sources,
  selectedId,
  onSelect,
}: {
  sources: SourceDescriptor[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div
      className="mb-4 grid gap-2 sm:grid-cols-2"
      role="radiogroup"
      aria-label="Model source"
    >
      {sources.map((s) => {
        const active = s.id === selectedId;
        return (
          <button
            key={s.id}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onSelect(s.id)}
            className={`flex flex-col gap-1 rounded-lg border p-3 text-left transition-colors ${
              active
                ? "border-primary bg-primary/5 ring-1 ring-primary/30"
                : "border-border bg-card hover:bg-muted/50"
            }`}
          >
            <span className="flex items-center gap-2">
              <LayersIcon
                className={`h-4 w-4 ${active ? "text-primary" : "text-muted-foreground"}`}
              />
              <span className="text-sm font-medium">{s.label}</span>
              {s.kind === "curated" && (
                <Badge variant="secondary" className="ml-auto text-[10px]">
                  Curated list
                </Badge>
              )}
            </span>
            <span className="text-xs text-muted-foreground">{s.description}</span>
          </button>
        );
      })}
    </div>
  );
}

/** Step 2/3 — the per-source browser: search/filter affordances driven by the
 *  source descriptor, then the result list with per-variant fit + pull. */
function SourceBrowser({ source }: { source: SourceDescriptor }) {
  const api = useModelManagerApi();
  const { connected } = useWebSocket();

  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [sort, setSort] = useState<CatalogSort>("downloads");
  const [recommendedOnly, setRecommendedOnly] = useState(false);
  const [compatibleOnly, setCompatibleOnly] = useState(false);
  // Selected size-class buckets (client-side filter over params_b). Empty ⇒
  // show all. Composes with Compatible (AND).
  const [sizeClasses, setSizeClasses] = useState<Set<SizeClass>>(new Set());
  const toggleSizeClass = useCallback((sc: SizeClass) => {
    setSizeClasses((prev) => {
      const next = new Set(prev);
      if (next.has(sc)) next.delete(sc);
      else next.add(sc);
      return next;
    });
  }, []);

  // Whether sort applies for this source (HF yes, curated Ollama no). A curated
  // source ignores it server-side anyway, but hiding the control is clearer.
  const effectiveSort = source.supports_sort ? sort : "downloads";

  // Best fit verdict per model, reported up by each ModelRow once its variants
  // load. The "Compatible" filter reads this to hide models whose best loaded
  // variant won't fit. Honest caveat: a model's fit is only known after its row
  // has been expanded (variants are fetched lazily), so list-level filtering
  // only acts on already-loaded models. Per-variant filtering inside a row is
  // exact.
  const [modelFit, setModelFit] = useState<Record<string, FitVerdict>>({});
  const reportFit = useCallback((modelId: string, fit: FitVerdict) => {
    setModelFit((prev) => (prev[modelId] === fit ? prev : { ...prev, [modelId]: fit }));
  }, []);

  const { data, isFetching, isError } = useQuery<SourceSearchResponse>({
    queryKey: [
      "model_manager",
      "source",
      source.id,
      submittedQuery,
      effectiveSort,
      recommendedOnly,
    ],
    queryFn: () =>
      api.searchSource(source.id, submittedQuery, effectiveSort, 25, recommendedOnly),
    enabled: connected,
    staleTime: 60_000,
  });

  const all: SourceModel[] = data?.models ?? [];
  const models = all.filter((m) => {
    if (compatibleOnly && modelFit[m.id] === "wont-fit") return false;
    if (sizeClasses.size > 0) {
      const sc = sizeClassOf(m.params_b);
      if (sc === null || !sizeClasses.has(sc)) return false;
    }
    return true;
  });

  return (
    <div>
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
            placeholder={source.search_placeholder}
            className="pl-8"
            aria-label={`Search ${source.label}`}
          />
        </form>

        {source.supports_sort && (
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
        )}

        <div className="flex items-center gap-2">
          <Switch
            id="compatible-only"
            checked={compatibleOnly}
            onCheckedChange={setCompatibleOnly}
          />
          <Label
            htmlFor="compatible-only"
            className="text-xs text-muted-foreground whitespace-nowrap"
            title={`Hide ${source.variant_noun}s (and models) that won't run on this host.`}
          >
            Compatible
          </Label>
        </div>

        {source.supports_recommended_only && (
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
        )}
      </div>

      {/* Size-class quick filter (client-side over params_b). */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">Size:</span>
        {SIZE_CLASSES.map((sc) => {
          const active = sizeClasses.has(sc.value);
          return (
            <button
              key={sc.value}
              type="button"
              onClick={() => toggleSizeClass(sc.value)}
              aria-pressed={active}
              title={sc.title}
              className={`rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors ${
                active
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border bg-card text-muted-foreground hover:bg-muted"
              }`}
            >
              {sc.label}
            </button>
          );
        })}
        {source.supports_sort && isDerivedSort(sort) && (
          <span className="ml-auto text-[11px] text-muted-foreground">
            “{sort === "powerful" ? "Most powerful" : "Smallest"}” re-ranks the
            top matches by parameter count (HF has no params sort).
          </span>
        )}
        {source.kind === "curated" && (
          <span className="ml-auto text-[11px] text-muted-foreground">
            Curated list — {source.label} has no public search API, so this is a
            hand-picked set, not a full catalog.
          </span>
        )}
      </div>

      {isError ? (
        <CatalogErrorPlaceholder />
      ) : isFetching && all.length === 0 ? (
        <LoadingPlaceholder label={`Searching ${source.label}…`} />
      ) : models.length === 0 ? (
        <CatalogEmptyPlaceholder
          recommendedOnly={recommendedOnly && source.supports_recommended_only}
        />
      ) : (
        <ul className="divide-y rounded-lg border bg-card">
          {models.map((model) => (
            <ModelRow
              key={model.id}
              source={source}
              model={model}
              listVariants={api.listSourceVariants}
              compatibleOnly={compatibleOnly}
              onFitResolved={reportFit}
            />
          ))}
        </ul>
      )}
    </div>
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

/** One model row in a source's result list. Expands to the model's pullable
 *  variants (HF quants / Ollama size tags), each with a fit verdict and a pull
 *  action. Source-neutral: the variant noun + popularity columns adapt to the
 *  source descriptor. */
function ModelRow({
  source,
  model,
  listVariants,
  compatibleOnly,
  onFitResolved,
}: {
  source: SourceDescriptor;
  model: SourceModel;
  listVariants: (
    source: string,
    modelId: string,
  ) => Promise<SourceVariantsResponse>;
  compatibleOnly: boolean;
  onFitResolved: (modelId: string, fit: FitVerdict) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const { data, isFetching, isError } = useQuery<SourceVariantsResponse>({
    queryKey: ["model_manager", "source", source.id, "variants", model.id],
    queryFn: () => listVariants(source.id, model.id),
    enabled: expanded,
    staleTime: 5 * 60_000,
  });

  const variants: SourceVariant[] = data?.variants ?? [];

  // Report this model's best-case fit up so the Compatible filter can act on it.
  useEffect(() => {
    if (data) onFitResolved(model.id, bestFit(variants.map((v) => v.fit)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  // When Compatible is on, drop the variants that won't fit — keep unknowns.
  const shownVariants = compatibleOnly
    ? variants.filter((v) => v.fit !== "wont-fit")
    : variants;

  // Popularity columns only make sense for a source with real signals (HF).
  const hasPopularity = source.kind === "search";

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
          <span className="font-mono text-sm truncate">
            {model.name ?? model.id}
          </span>
          <span
            className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground tabular-nums"
            title={
              model.params_b !== null
                ? `≈${model.params_b}B parameters`
                : "Parameter count unknown."
            }
          >
            {paramChip(model.params_b)}
          </span>
          {model.recommended && source.supports_recommended_only && (
            <Badge variant="success" className="shrink-0 gap-1">
              <StarIcon className="h-3 w-3" />
              Recommended
            </Badge>
          )}
        </div>
        {hasPopularity && (
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
        )}
      </button>

      {expanded && (
        <div className="mt-3 ml-6 rounded-md border bg-muted/30 p-2">
          {isError ? (
            <p className="px-2 py-1 text-xs text-rose-600">
              Couldn&apos;t load {source.variant_noun}s for this model.
            </p>
          ) : isFetching ? (
            <p className="px-2 py-1 text-xs text-muted-foreground">
              Loading {source.variant_noun}s…
            </p>
          ) : variants.length === 0 ? (
            <p className="px-2 py-1 text-xs text-muted-foreground">
              No {source.variant_noun}s found for this model.
            </p>
          ) : shownVariants.length === 0 ? (
            <p className="px-2 py-1 text-xs text-muted-foreground">
              No compatible {source.variant_noun}s — turn off “Compatible” to see
              all {variants.length}.
            </p>
          ) : (
            <ul className="divide-y divide-border/60">
              {shownVariants.map((v) => (
                <li
                  key={v.variant_id}
                  className="flex items-center justify-between gap-3 px-2 py-1.5"
                >
                  <span className="font-mono text-xs truncate">{v.label}</span>
                  <div className="flex shrink-0 items-center gap-2">
                    <FitBadge fit={v.fit} />
                    <span
                      className="text-xs text-muted-foreground tabular-nums"
                      title={
                        v.size_estimated
                          ? "Estimated size (Ollama serves no per-tag size); fit is approximate."
                          : undefined
                      }
                    >
                      {v.size_estimated && v.size_bytes !== null ? "~" : ""}
                      {humanSize(v.size_bytes)}
                    </span>
                    <PullButton variant={v} repoId={model.id} source={source} />
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

/** Per-variant Pull button (source-neutral).
 *
 * Pulls the variant's exact ``pull_ref`` into Ollama (``hf.co/<repo>:<quant>``
 * for HF, a bare registry tag like ``llama3.3:70b`` for Ollama). Progress is
 * coarse by design (gilbert#40): the RPC is awaited to completion behind a
 * 10-minute keepalive backstop, so this shows a "Pulling…" spinner while in
 * flight and an "Installed" tick on success. On success the installed-models
 * query is invalidated so the Installed section refetches.
 *
 * Disabled for a non-pullable variant (no buildable ref, or a quant tag Ollama
 * doesn't recognize), which would otherwise 400. */
function PullButton({
  variant,
  repoId,
  source,
}: {
  variant: SourceVariant;
  repoId: string;
  source: SourceDescriptor;
}) {
  const api = useModelManagerApi();
  const queryClient = useQueryClient();

  const pull = useMutation({
    // Only HF benefits from server-side per-model-config seeding from the repo
    // id; for Ollama the installed tag carries no HF metadata, so omit repoId.
    mutationFn: () =>
      api.pullRef(
        variant.pull_ref,
        source.id === "huggingface" ? repoId : undefined,
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: INSTALLED_QUERY_KEY }),
  });

  if (!variant.pullable || !variant.pull_ref) {
    return (
      <Button
        variant="outline"
        size="sm"
        className="h-6 px-2 text-[11px]"
        disabled
        title={`This ${source.variant_noun} can't be pulled into Ollama — try a standard one.`}
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

  // Surface the real failure message (from the server's gilbert.error) inline,
  // not just a bare "Retry" — a multi-GB pull that genuinely fails (bad ref,
  // not-found, daemon down) deserves an honest reason next to the button.
  const errorMessage =
    pull.isError && pull.error instanceof Error ? pull.error.message : null;

  return (
    <div className="flex flex-col items-end gap-0.5">
      <Button
        variant="outline"
        size="sm"
        className="h-6 gap-1 px-2 text-[11px]"
        onClick={() => pull.mutate()}
        disabled={pull.isPending}
        title={
          errorMessage
            ? `${errorMessage} — click to retry.`
            : `Pull ${variant.label} into Ollama`
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
      {errorMessage && (
        <span
          className="max-w-[14rem] truncate text-right text-[10px] text-rose-600"
          title={errorMessage}
        >
          {errorMessage}
        </span>
      )}
    </div>
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
