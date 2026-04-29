"""Tests for ClashAPI retry / backoff logic.

Mocks ``httpx.AsyncClient.get`` so the suite can verify retry behaviour
without hitting the real Clash of Clans API. ``asyncio.sleep`` is patched
to avoid real delays.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from coc_tracker.api import ClashAPI


def _resp(status: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {})


@pytest.fixture
def no_sleep():
    with patch("coc_tracker.api.asyncio.sleep", new=AsyncMock()):
        yield


@pytest.mark.asyncio
async def test_get_returns_json_on_200():
    api = ClashAPI("token")
    with patch.object(
        api._client,
        "get",
        new=AsyncMock(return_value=_resp(200, {"hello": "world"})),
    ):
        result = await api._get("https://example.com")
    assert result == {"hello": "world"}
    await api.aclose()


@pytest.mark.asyncio
async def test_get_retries_on_500_then_succeeds(no_sleep):
    api = ClashAPI("token")
    seq = [_resp(500), _resp(503), _resp(200, {"ok": True})]
    with patch.object(api._client, "get", new=AsyncMock(side_effect=seq)):
        result = await api._get("https://example.com")
    assert result == {"ok": True}
    await api.aclose()


@pytest.mark.asyncio
async def test_get_gives_up_after_max_retries(no_sleep):
    api = ClashAPI("token")
    with patch.object(api._client, "get", new=AsyncMock(return_value=_resp(503))):
        result = await api._get("https://example.com")
    assert result is None
    await api.aclose()


@pytest.mark.asyncio
async def test_get_returns_none_on_404_without_retry():
    api = ClashAPI("token")
    mock = AsyncMock(return_value=_resp(404))
    with patch.object(api._client, "get", new=mock):
        result = await api._get("https://example.com")
    assert result is None
    assert mock.call_count == 1  # no retry on non-retryable status
    await api.aclose()


@pytest.mark.asyncio
async def test_get_retries_on_network_error(no_sleep):
    api = ClashAPI("token")
    seq = [httpx.ConnectError("connection refused"), _resp(200, {"ok": True})]
    mock = AsyncMock(side_effect=seq)
    with patch.object(api._client, "get", new=mock):
        result = await api._get("https://example.com")
    assert result == {"ok": True}
    assert mock.call_count == 2
    await api.aclose()


@pytest.mark.asyncio
async def test_clan_tag_url_encoded():
    api = ClashAPI("token")
    captured: dict[str, str] = {}

    async def fake_get(url: str) -> httpx.Response:
        captured["url"] = url
        return _resp(200, {"items": []})

    with patch.object(api._client, "get", new=fake_get):
        await api.get_clan_members("#ABC123")
    assert "%23ABC123" in captured["url"]
    await api.aclose()
