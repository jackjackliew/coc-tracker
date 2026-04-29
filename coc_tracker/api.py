"""Async Clash of Clans API client (httpx-based)."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import COC_API_BASE, HTTP_TIMEOUT

logger = logging.getLogger(__name__)


class ClashAPI:
    """Async wrapper around the public Clash of Clans REST API."""

    def __init__(self, token: str, timeout: float = HTTP_TIMEOUT):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._client = httpx.AsyncClient(headers=self.headers, timeout=timeout, http2=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ClashAPI":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _get(self, url: str) -> Optional[dict]:
        try:
            r = await self._client.get(url)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                logger.warning(f"Rate limited on {url}; CoC API returned 429")
                return None
            logger.warning(f"GET {url} returned status {r.status_code}")
            return None
        except httpx.HTTPError as e:
            logger.warning(f"GET failed [{url}]: {e}")
            return None

    @staticmethod
    def _encode_tag(clan_tag: str) -> str:
        return clan_tag.replace("#", "%23")

    async def get_clan_members(self, clan_tag: str) -> Optional[dict]:
        return await self._get(f"{COC_API_BASE}/clans/{self._encode_tag(clan_tag)}/members")

    async def get_clan_info(self, clan_tag: str) -> Optional[dict]:
        return await self._get(f"{COC_API_BASE}/clans/{self._encode_tag(clan_tag)}")

    async def get_season_key(self) -> Optional[str]:
        data = await self._get(f"{COC_API_BASE}/goldpass/seasons/current")
        if data and "startTime" in data:
            try:
                return data["startTime"][:8]
            except Exception:
                pass
        return None
