"""Hugging Face Hub catalog client — browse GGUF models for the manager.

A thin, defensive client over the public Hugging Face Hub REST API
(``https://huggingface.co/api``) used by the model **manager** to browse
GGUF repos (the scope of what Ollama can pull: ``ollama pull hf.co/<repo>``)
— PRD gilbert#32, S6 gilbert#38.

Two operations:

- :func:`search` — list GGUF repos with HF-native sort/search, each tagged
  with whether it's in Gilbert's recommended overlay.
- :func:`list_quants` — for one repo, enumerate its ``*.gguf`` files with a
  parsed quantization label and per-file size.

No API key needed for public read access. Every parse is best-effort: the
Hub occasionally omits fields, and repos vary wildly in how they name
files, so missing data degrades to ``None``/empty rather than raising.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .recommended import RECOMMENDED_MODELS, recommended_repo_ids

__all__ = [
    "CatalogModel",
    "Quant",
    "HF_API_BASE",
    "HF_RESOLVE_BASE",
    "SORT_KEY_MAP",
    "DERIVED_SORTS",
    "OLLAMA_PULLABLE_QUANTS",
    "search",
    "search_recommended",
    "list_quants",
    "fetch_context_window",
    "parse_quant_label",
    "parse_param_count",
    "is_pullable_quant",
]

HF_API_BASE = "https://huggingface.co/api"
# Raw-file CDN base — ``<base>/<repo>/resolve/main/<path>`` serves a repo file
# (e.g. ``config.json``) directly. Used best-effort for context-window seeding.
HF_RESOLVE_BASE = "https://huggingface.co"

# Keys a Hugging Face ``config.json`` uses for the model's max context length,
# in preference order. ``max_position_embeddings`` is the modern Transformers
# field; ``n_positions`` / ``n_ctx`` are older GPT-style names. First positive
# integer wins.
_CONTEXT_WINDOW_KEYS = ("max_position_embeddings", "n_positions", "n_ctx")

# Map the UI's sort keys → the Hugging Face Hub API's ``sort`` field names.
# ``size`` has no server-side equivalent (HF doesn't sort repos by file
# size), so it's intentionally absent here — the Browse UI sorts by size
# client-side over the per-quant sizes it already fetches.
SORT_KEY_MAP: dict[str, str] = {
    "downloads": "downloads",
    "likes": "likes",
    "trending": "trendingScore",
    "recent": "lastModified",
}

_DEFAULT_SORT = "downloads"

# Derived (client/server re-rank) sort keys. HF's ``/api/models`` can only sort
# by downloads / likes / trendingScore / lastModified — there is NO param-count
# sort on the Hub. So "Most powerful" / "Smallest" are a DERIVED re-rank: fetch
# a LARGER page ordered by a real HF field (downloads) and re-sort it by the
# parameter count parsed from the repo id. Honest caveat: this re-ranks the
# *top matches*, not all of HF — a tiny model that's unpopular enough to fall
# outside the base page won't surface. ``True`` ⇒ descending params (powerful),
# ``False`` ⇒ ascending params (smallest).
DERIVED_SORTS: dict[str, bool] = {
    "powerful": True,
    "smallest": False,
}

# Size of the base page fetched before a derived re-rank. Bigger than the usual
# limit so the re-rank has a meaningful candidate pool to reorder.
_DERIVED_PAGE_SIZE = 100

# Quantization tags Ollama accepts as a pull reference (``hf.co/<repo>:<tag>``)
# — the canonical llama.cpp k-quant / legacy / integer-quant / float / IQ
# schemes. A GGUF file labeled with anything outside this set (e.g. a community
# ``Q8_K_P``) makes ``ollama pull`` 400 with "not a valid quantization scheme",
# so we mark such quants non-pullable and never hand them to the runtime.
# Compared case-insensitively (see :func:`is_pullable_quant`).
OLLAMA_PULLABLE_QUANTS: frozenset[str] = frozenset(
    {
        "Q2_K",
        "Q3_K_S",
        "Q3_K_M",
        "Q3_K_L",
        "Q4_0",
        "Q4_1",
        "Q4_K_S",
        "Q4_K_M",
        "Q5_0",
        "Q5_1",
        "Q5_K_S",
        "Q5_K_M",
        "Q6_K",
        "Q8_0",
        "F16",
        "BF16",
        "F32",
        "IQ1_S",
        "IQ1_M",
        "IQ2_XXS",
        "IQ2_XS",
        "IQ2_S",
        "IQ2_M",
        "IQ3_XXS",
        "IQ3_XS",
        "IQ3_S",
        "IQ3_M",
        "IQ4_XS",
        "IQ4_NL",
    }
)

# Quantization labels embedded in GGUF filenames, e.g.
# ``Llama-3.3-70B-Instruct-Q4_K_M.gguf`` → ``Q4_K_M``. Covers the common
# k-quant / legacy / integer-quant / float forms. Case-insensitive; the
# matched label is upper-cased for display.
_QUANT_RE = re.compile(
    r"(IQ\d+_[A-Z0-9_]+|Q\d+_K_[A-Z]+|Q\d+_K|Q\d+_\d+|Q\d+|F16|F32|BF16)",
    re.IGNORECASE,
)

# Parameter-count tokens embedded in repo ids / filenames. Matches a number
# (optionally an ``<experts>x<size>`` MoE form, optionally an ``-A<active>``
# split), then a ``B`` (billions) or ``M`` (millions) unit. Examples:
# ``7B`` / ``70B`` / ``3.8B`` / ``500M`` / ``8x7B`` / ``35B-A3B``. The unit is
# required so a bare version number (``3.3``) is never mistaken for params.
# Word-boundary-anchored so we don't match the ``b`` inside e.g. ``Turbo``.
_PARAM_RE = re.compile(
    r"(?<![A-Za-z0-9.])"  # not preceded by an alnum / dot (avoid version tails)
    r"(?:(\d+)x)?"  # optional MoE expert count: 8x…
    r"(\d+(?:\.\d+)?)"  # the size number: 7, 70, 3.8
    r"\s*([BM])"  # unit: Billions or Millions
    r"(?![A-Za-z0-9])",  # not followed by an alnum (so ``8B`` not ``8Bit``)
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CatalogModel:
    """One GGUF repo from the Hugging Face Hub catalog.

    - ``id`` — the repo id (``<owner>/<name>``).
    - ``downloads`` / ``likes`` — HF popularity signals (0 when absent).
    - ``last_modified`` — ISO-8601 timestamp string, or ``None``.
    - ``recommended`` — True iff ``id`` is in Gilbert's recommended overlay.
    - ``params_b`` — approximate parameter count in **billions** parsed from
      the id (``None`` when no recognizable token). Drives the derived
      "powerful" / "smallest" sort and the UI param chip + size-class filter.
    """

    id: str
    downloads: int
    likes: int
    last_modified: str | None
    recommended: bool
    params_b: float | None = None


@dataclass(frozen=True)
class Quant:
    """One ``*.gguf`` file in a repo — a single quantization.

    - ``filename`` — the file path within the repo.
    - ``quant_label`` — parsed quant tag (``Q4_K_M``, ``Q8_0``, ``F16``…),
      or ``None`` when the filename has no recognizable quant marker.
    - ``size_bytes`` — file size in bytes, or ``None`` when HF doesn't report
      it (the Hub only returns blob sizes when asked with ``blobs=true``).
    - ``pullable`` — True iff ``quant_label`` is a quantization tag Ollama
      accepts as a pull reference (see :data:`OLLAMA_PULLABLE_QUANTS`). A junk
      / community tag is non-pullable, so the UI disables the Pull button.
    """

    filename: str
    quant_label: str | None
    size_bytes: int | None
    pullable: bool = False


def _map_sort(sort: str) -> str:
    """Map a UI sort key to an HF ``sort`` field, defaulting defensively.

    A derived sort (``powerful`` / ``smallest``, which HF can't do server-side)
    falls back to the default base field — the param re-rank happens after the
    fetch, not on the Hub.
    """
    if sort in DERIVED_SORTS:
        return _DEFAULT_SORT
    return SORT_KEY_MAP.get(sort, _DEFAULT_SORT)


def parse_quant_label(filename: str) -> str | None:
    """Parse a quantization label from a GGUF filename, or ``None``.

    Picks the last quant-looking token (quant markers sit near the end of
    GGUF filenames, after the model name which may itself contain digits).
    """
    matches = _QUANT_RE.findall(filename)
    if not matches:
        return None
    return matches[-1].upper()


def parse_param_count(name: str) -> float | None:
    """Approximate parameter count in **billions** from a repo id / filename.

    Best-effort and heuristic — the param count isn't a Hub field, so we read
    it from the name. Handles:

    - ``7B`` / ``70B`` → 7 / 70.
    - decimals ``3.8B`` → 3.8.
    - ``M`` suffix ``500M`` → 0.5 (millions → billions).
    - **MoE** ``8x7B`` → 56 (total = experts × per-expert size).
    - active/total splits ``35B-A3B`` → 35 (the ``A3B`` *active* count is
      ignored; the larger total token wins).

    Returns ``None`` when there's no recognizable param token — a bare version
    number like ``Llama-3.3`` has no ``B``/``M`` unit and so never matches.

    When several tokens match, the **largest** billions value is taken: that's
    the model's total size (the active-expert count in an ``A3B`` split is
    always the smaller token, so picking the max drops it correctly).
    """
    best: float | None = None
    for experts, size, unit in _PARAM_RE.findall(name):
        try:
            value = float(size)
        except ValueError:  # pragma: no cover - regex guarantees a number
            continue
        if experts:
            try:
                value *= float(experts)
            except ValueError:  # pragma: no cover
                pass
        if unit.upper() == "M":
            value /= 1000.0
        if best is None or value > best:
            best = value
    return best


def is_pullable_quant(quant_label: str | None) -> bool:
    """True iff Ollama accepts ``quant_label`` as a ``hf.co/<repo>:<tag>`` pull
    reference (case-insensitive membership in :data:`OLLAMA_PULLABLE_QUANTS`).

    A ``None`` / empty / unrecognized label is **not** pullable — handing it to
    ``ollama pull`` 400s with "not a valid quantization scheme".
    """
    if not quant_label:
        return False
    return quant_label.upper() in OLLAMA_PULLABLE_QUANTS


async def search(
    query: str,
    sort: str,
    limit: int,
    *,
    recommended_only: bool = False,
    client: httpx.AsyncClient | None = None,
) -> list[CatalogModel]:
    """Search the Hugging Face Hub for GGUF repos.

    Issues ``GET /api/models`` filtered to ``gguf`` repos, with the UI's
    ``sort`` mapped to HF's field name and descending order. Each result is
    tagged ``recommended`` from the overlay and carries a best-effort
    ``params_b``. Defensive: unexpected payload shapes yield an empty list
    rather than raising; per-row field gaps degrade to zeros/``None``.

    When ``recommended_only`` is set the generic Hub query is bypassed
    entirely and the curated overlay is returned directly (see
    :func:`search_recommended`) — sourcing it server-side guarantees the
    curated set always shows, instead of post-filtering a generic page where
    the ~10 curated repos almost never appear.

    For the derived ``powerful`` (params ↓) / ``smallest`` (params ↑) sorts —
    which HF can't do, params not being a Hub field — a LARGER base page is
    fetched ordered by downloads, then re-ranked client-side by ``params_b``
    (unknown params sink to the bottom) and truncated to ``limit``. This
    re-ranks the **top matches, not all of HF**: a model outside the base page
    won't surface. The other sorts stay pass-through (the page order is HF's).

    ``client`` is injectable for testing; otherwise a short-lived client is
    created and closed per call.
    """
    if recommended_only:
        return await search_recommended(client=client)

    derived = sort in DERIVED_SORTS
    fetch_limit = max(int(limit), _DERIVED_PAGE_SIZE) if derived else int(limit)
    params = {
        "filter": "gguf",
        "search": query,
        "sort": _map_sort(sort),
        "direction": "-1",
        "limit": str(fetch_limit),
    }

    rec_ids = recommended_repo_ids()
    data = await _get_json(f"{HF_API_BASE}/models", params, client)
    if not isinstance(data, list):
        return []

    results: list[CatalogModel] = []
    for entry in data:
        model = _parse_model_entry(entry, rec_ids)
        if model is not None:
            results.append(model)

    if derived:
        descending = DERIVED_SORTS[sort]
        # Sort by params with unknowns (None) always last regardless of
        # direction: rank tuple is (is_unknown, signed_value). Truncate to the
        # requested limit after the re-rank.
        results.sort(key=lambda m: _param_sort_key(m.params_b, descending))
        results = results[: int(limit)]

    return results


def _param_sort_key(params_b: float | None, descending: bool) -> tuple[int, float]:
    """Sort key for the derived param re-rank — unknowns sink, known params
    order by the requested direction."""
    if params_b is None:
        return (1, 0.0)
    return (0, -params_b if descending else params_b)


async def search_recommended(
    *,
    client: httpx.AsyncClient | None = None,
) -> list[CatalogModel]:
    """Return Gilbert's curated overlay directly as ``CatalogModel``s.

    Sourced from :data:`RECOMMENDED_MODELS`, independent of any generic Hub
    query — so the curated set is ALWAYS present (the old client-side
    post-filter over a generic top-N page almost never surfaced these repos).
    Downloads / likes / last_modified are best-effort 0 / None (popularity
    isn't what the overlay is about); ``params_b`` is parsed from the id so the
    chip + size-class filter still work. No network call — ``client`` is
    accepted only for signature parity with :func:`search`.
    """
    return [
        CatalogModel(
            id=m.repo_id,
            downloads=0,
            likes=0,
            last_modified=None,
            recommended=True,
            params_b=parse_param_count(m.repo_id),
        )
        for m in RECOMMENDED_MODELS
    ]


def _parse_model_entry(
    entry: object,
    rec_ids: frozenset[str],
) -> CatalogModel | None:
    """Parse one ``/api/models`` row into a ``CatalogModel`` defensively."""
    if not isinstance(entry, dict):
        return None
    # HF returns the repo id under ``id`` (and historically ``modelId``).
    repo_id = entry.get("id") or entry.get("modelId")
    if not isinstance(repo_id, str) or not repo_id:
        return None
    return CatalogModel(
        id=repo_id,
        downloads=_as_int(entry.get("downloads")),
        likes=_as_int(entry.get("likes")),
        last_modified=_as_opt_str(entry.get("lastModified")),
        recommended=repo_id in rec_ids,
        params_b=parse_param_count(repo_id),
    )


async def list_quants(
    model_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[Quant]:
    """List the ``*.gguf`` quantizations available in a repo.

    Fetches ``GET /api/models/<id>?blobs=true`` (``blobs=true`` makes the
    Hub include per-file sizes), keeps only ``.gguf`` siblings, and parses a
    quant label + size for each. Defensive: a repo with no gguf files (or an
    unexpected payload) yields an empty list rather than raising.
    """
    data = await _get_json(
        f"{HF_API_BASE}/models/{model_id}",
        {"blobs": "true"},
        client,
    )
    if not isinstance(data, dict):
        return []

    siblings = data.get("siblings")
    if not isinstance(siblings, list):
        return []

    quants: list[Quant] = []
    for sib in siblings:
        quant = _parse_sibling(sib)
        if quant is not None:
            quants.append(quant)
    return quants


def _parse_sibling(sib: object) -> Quant | None:
    """Parse one repo sibling into a ``Quant`` if it's a gguf file."""
    if not isinstance(sib, dict):
        return None
    filename = sib.get("rfilename")
    if not isinstance(filename, str) or not filename.lower().endswith(".gguf"):
        return None
    # The Hub reports blob size under ``size`` (with ``blobs=true``); some
    # responses nest it under ``lfs.size`` for LFS-tracked files.
    size = sib.get("size")
    if size is None:
        lfs = sib.get("lfs")
        if isinstance(lfs, dict):
            size = lfs.get("size")
    quant_label = parse_quant_label(filename)
    return Quant(
        filename=filename,
        quant_label=quant_label,
        size_bytes=_as_opt_int(size),
        pullable=is_pullable_quant(quant_label),
    )


async def fetch_context_window(
    model_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> int | None:
    """Best-effort context window (max sequence length) for a repo, or ``None``.

    Reads the repo's ``config.json`` from the HF raw-file CDN
    (``/<repo>/resolve/main/config.json``) and returns the first positive
    integer found under :data:`_CONTEXT_WINDOW_KEYS`
    (``max_position_embeddings`` / ``n_positions`` / ``n_ctx``).

    **Best-effort by design**: GGUF quant repos frequently have no
    ``config.json`` at all (the metadata lives inside the .gguf header,
    which we don't parse here), the field may be absent, or the CDN may be
    unreachable. Any of those returns ``None`` — the caller seeds the
    per-model ``context_window`` only when this is known and never fails the
    pull over it. Reliability is therefore partial: present for many
    Transformers-derived repos that ship a config.json, absent for the rest.
    """
    url = f"{HF_RESOLVE_BASE}/{model_id}/resolve/main/config.json"
    data = await _get_json(url, {}, client)
    if not isinstance(data, dict):
        return None
    for key in _CONTEXT_WINDOW_KEYS:
        value = data.get(key)
        ctx = _as_opt_int(value)
        if ctx is not None and ctx > 0:
            return ctx
    return None


async def _get_json(
    url: str,
    params: dict[str, str],
    client: httpx.AsyncClient | None,
) -> object:
    """GET ``url`` and return parsed JSON, or ``None`` on any failure.

    Reuses an injected client when given; otherwise opens and closes a
    short-lived one. Network errors, non-2xx status, and malformed JSON all
    collapse to ``None`` so callers degrade gracefully.
    """
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None
    finally:
        if own_client:
            await client.aclose()


def _as_int(value: object) -> int:
    """Coerce a JSON value to int, defaulting to 0 on anything odd."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _as_opt_int(value: object) -> int | None:
    """Coerce a JSON value to int, or ``None`` when absent/uncoercible."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_opt_str(value: object) -> str | None:
    """Return ``value`` if it's a non-empty string, else ``None``."""
    if isinstance(value, str) and value:
        return value
    return None
