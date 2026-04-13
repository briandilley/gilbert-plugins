"""Shared async HTTP client for Sonarr/Radarr v3 APIs."""

from __future__ import annotations

from typing import Any

import httpx


class ArrClient:
    """Async HTTP client for *arr APIs (Sonarr, Radarr).

    Thin wrapper around httpx that prefixes the v3 API path and injects
    the ``X-Api-Key`` header. ``available`` is true when both a URL and
    API key have been configured.
    """

    def __init__(self, name: str, url: str, api_key: str) -> None:
        self.name = name
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._http: httpx.AsyncClient | None = None
        if self._url and self._api_key:
            self._http = httpx.AsyncClient(
                base_url=f"{self._url}/api/v3",
                headers={"X-Api-Key": self._api_key},
                timeout=30.0,
            )

    @property
    def available(self) -> bool:
        return self._http is not None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._http is None:
            raise RuntimeError(f"{self.name} client not configured")
        resp = await self._http.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        if self._http is None:
            raise RuntimeError(f"{self.name} client not configured")
        resp = await self._http.post(path, json=data or {})
        resp.raise_for_status()
        return resp.json()

    async def put(self, path: str, data: dict[str, Any] | None = None) -> Any:
        if self._http is None:
            raise RuntimeError(f"{self.name} client not configured")
        resp = await self._http.put(path, json=data or {})
        resp.raise_for_status()
        return resp.json()

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._http is None:
            raise RuntimeError(f"{self.name} client not configured")
        resp = await self._http.delete(path, params=params)
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
