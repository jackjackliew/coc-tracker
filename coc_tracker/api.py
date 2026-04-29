"""Async Clash of Clans API client (httpx-based) with retry + backoff."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import COC_API_BASE, HTTP_TIMEOUT

logger = logging.getLogger(__name__)

# Retry policy for transient errors (5xx, network errors, 429 rate-limit).
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0  # exponential: 1s, 2s, 4s


class ClashAPI:
    """Async wrapper around the public Clash of Clans REST API.

    Retries transient errors (429, 5xx, network) with exponential backoff.
    Returns ``None`` on non-recoverable failures so callers can no-op the cycle.
    """

    def __init__(self, token: str, timeout: float = HTTP_TIMEOUT):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._client = httpx.AsyncClient(headers=self.headers, timeout=timeout, http2=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ClashAPI:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _get(self, url: str) -> dict | None:
        last_error: str | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                r = await self._client.get(url)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in _RETRY_STATUS:
                    last_error = f"status {r.status_code}"
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
                # Non-retryable error (auth, not found, etc.)
                logger.warning(f"GET {url} returned non-retryable status {r.status_code}")
                return None
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_error = f"{type(e).__name__}: {e}"
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
            except httpx.HTTPError as e:
                logger.warning(f"GET {url} raised non-retryable httpx error: {e}")
                return None
        logger.warning(f"GET {url} failed after {_MAX_RETRIES} attempts ({last_error})")
        return None

    @staticmethod
    def _encode_tag(clan_tag: str) -> str:
        return clan_tag.replace("#", "%23")

    async def get_clan_members(self, clan_tag: str) -> dict | None:
        return await self._get(f"{COC_API_BASE}/clans/{self._encode_tag(clan_tag)}/members")

    async def get_clan_info(self, clan_tag: str) -> dict | None:
        return await self._get(f"{COC_API_BASE}/clans/{self._encode_tag(clan_tag)}")

    async def get_season_key(self) -> str | None:
        data = await self._get(f"{COC_API_BASE}/goldpass/seasons/current")
        if data and "startTime" in data:
            try:
                return data["startTime"][:8]
            except Exception:
                pass
        return None
