"""Tests for the /reset command's admin gate.

The gate is the security boundary for a destructive command: a non-admin (or a
DM) must NOT be able to trigger a wipe. These tests lock that behaviour with
lightweight mocks rather than a live Telegram connection.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coc_tracker.handlers import group_admin_only


def _make_update(chat_type="supergroup", user_id=111):
    """Build a minimal Update-like object with an async reply_text."""
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_chat=SimpleNamespace(type=chat_type, id=-100123),
        effective_user=SimpleNamespace(id=user_id, username="tester"),
        message=message,
    )


def _make_context(admin_ids=(111,)):
    """Context whose bot reports the given user ids as chat admins."""
    admins = [SimpleNamespace(user=SimpleNamespace(id=uid)) for uid in admin_ids]
    bot = SimpleNamespace(get_chat_administrators=AsyncMock(return_value=admins))
    return SimpleNamespace(bot=bot)


@pytest.mark.asyncio
async def test_admin_passes_gate():
    called = {"hit": False}

    @group_admin_only
    async def inner(update, context):
        called["hit"] = True

    update = _make_update(user_id=111)
    await inner(update, _make_context(admin_ids=(111, 222)))
    assert called["hit"] is True


@pytest.mark.asyncio
async def test_non_admin_blocked():
    called = {"hit": False}

    @group_admin_only
    async def inner(update, context):
        called["hit"] = True

    update = _make_update(user_id=999)  # not in admin list
    await inner(update, _make_context(admin_ids=(111, 222)))
    assert called["hit"] is False
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_dm_blocked():
    called = {"hit": False}

    @group_admin_only
    async def inner(update, context):
        called["hit"] = True

    update = _make_update(chat_type="private", user_id=111)
    await inner(update, _make_context(admin_ids=(111,)))
    assert called["hit"] is False
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_fetch_failure_blocks():
    """If the admin list can't be fetched, fail closed — do not run the command."""
    called = {"hit": False}

    @group_admin_only
    async def inner(update, context):
        called["hit"] = True

    update = _make_update(user_id=111)
    context = SimpleNamespace(
        bot=SimpleNamespace(get_chat_administrators=AsyncMock(side_effect=RuntimeError("boom")))
    )
    await inner(update, context)
    assert called["hit"] is False
    update.message.reply_text.assert_awaited_once()
