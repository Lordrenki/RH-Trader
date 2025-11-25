"""External item catalog integration for autocomplete suggestions."""
from __future__ import annotations

import asyncio
from typing import Iterable, List, Optional

import aiohttp


class CatalogClient:
    """Client for fetching items from the ARDB catalog for autocomplete."""

    def __init__(
        self,
        base_url: str = "https://ardb.app",
        *,
        request_timeout: float = 5.0,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self._session = session
        self._owns_session = session is None

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def search_items(self, term: str, limit: int = 20) -> List[str]:
        """Return catalog item names that match the provided term.

        The ARDB catalog supports a search query; we defensively handle empty
        terms, HTTP failures, or unexpected payloads by returning an empty
        result rather than breaking Discord autocomplete responses.
        """

        cleaned = term.strip()
        if not cleaned:
            return []

        params = {"search": cleaned, "limit": str(limit)}
        try:
            async with self._get_session().get(
                f"{self.base_url}/api/items", params=params, timeout=self.request_timeout
            ) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return []

        entries: Iterable[object]
        if isinstance(payload, dict) and "items" in payload:
            entries = payload.get("items", []) or []
        else:
            entries = payload or []

        results: List[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                name = entry.get("name")
            else:
                name = str(entry)
            if name:
                results.append(str(name))
                if len(results) >= limit:
                    break
        return results
