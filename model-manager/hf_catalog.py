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

from .recommended import recommended_repo_ids

__all__ = [
    "CatalogModel",
    "Quant",
    "HF_API_BASE",
    "SORT_KEY_MAP",
    "search",
    "list_quants",
]

HF_API_BASE = "https://huggingface.co/api"

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

# Quantization labels embedded in GGUF filenames, e.g.
# ``Llama-3.3-70B-Instruct-Q4_K_M.gguf`` → ``Q4_K_M``. Covers the common
# k-quant / legacy / integer-quant / float forms. Case-insensitive; the
# matched label is upper-cased for display.
_QUANT_RE = re.compile(
    r"(IQ\d+_[A-Z0-9_]+|Q\d+_K_[A-Z]+|Q\d+_K|Q\d+_\d+|Q\d+|F16|F32|BF16)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CatalogModel:
    """One GGUF repo from the Hugging Face Hub catalog.

    - ``id`` — the repo id (``<owner>/<name>``).
    - ``downloads`` / ``likes`` — HF popularity signals (0 when absent).
    - ``last_modified`` — ISO-8601 timestamp string, or ``None``.
    - ``recommended`` — True iff ``id`` is in Gilbert's recommended overlay.
    """

    id: str
    downloads: int
    likes: int
    last_modified: str | None
    recommended: bool


@dataclass(frozen=True)
class Quant:
    """One ``*.gguf`` file in a repo — a single quantization.

    - ``filename`` — the file path within the repo.
    - ``quant_label`` — parsed quant tag (``Q4_K_M``, ``Q8_0``, ``F16``…),
      or ``None`` when the filename has no recognizable quant marker.
    - ``size_bytes`` — file size in bytes, or ``None`` when HF doesn't report
      it (the Hub only returns blob sizes when asked with ``blobs=true``).
    """

    filename: str
    quant_label: str | None
    size_bytes: int | None


def _map_sort(sort: str) -> str:
    """Map a UI sort key to an HF ``sort`` field, defaulting defensively."""
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


async def search(
    query: str,
    sort: str,
    limit: int,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[CatalogModel]:
    """Search the Hugging Face Hub for GGUF repos.

    Issues ``GET /api/models`` filtered to ``gguf`` repos, with the UI's
    ``sort`` mapped to HF's field name and descending order. Each result is
    tagged ``recommended`` from the overlay. Defensive: unexpected payload
    shapes yield an empty list rather than raising; per-row field gaps
    degrade to zeros/``None``.

    ``client`` is injectable for testing; otherwise a short-lived client is
    created and closed per call.
    """
    params = {
        "filter": "gguf",
        "search": query,
        "sort": _map_sort(sort),
        "direction": "-1",
        "limit": str(int(limit)),
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
    return results


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
    return Quant(
        filename=filename,
        quant_label=parse_quant_label(filename),
        size_bytes=_as_opt_int(size),
    )


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
