"""Button taps on old bot messages must post a fresh reply, not edit in place.

Telegram keeps an edited message's original send date. Refreshing a weeks-old
leaderboard in place made live data look like it was from that day (a clanmate
saw "October 17" on 2 Nov). These tests lock in: recent message → edit in place;
old or inaccessible message → new message at the bottom of the chat.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coc_tracker.config import STALE_MESSAGE_SECONDS
from coc_tracker.handlers import _edit_message
from coc_tracker.storage import DonationStorage
from coc_tracker.tracker import DonationTracker


def _make_query(age_seconds):
    sent = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    chat = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(date=sent, chat=chat)
    return SimpleNamespace(message=message, edit_message_text=AsyncMock())


@pytest.mark.asyncio
async def test_recent_message_is_edited_in_place():
    query = _make_query(age_seconds=30)
    await _edit_message(query, "hello", reply_markup="kb")
    query.edit_message_text.assert_awaited_once_with("hello", reply_markup="kb")
    query.message.chat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_message_gets_fresh_reply():
    query = _make_query(age_seconds=STALE_MESSAGE_SECONDS + 60)
    await _edit_message(query, "hello", reply_markup="kb")
    query.message.chat.send_message.assert_awaited_once_with("hello", reply_markup="kb")
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_weeks_old_message_gets_fresh_reply():
    query = _make_query(age_seconds=18 * 24 * 3600)  # 17 Oct → 4 Nov
    await _edit_message(query, "hello")
    query.message.chat.send_message.assert_awaited_once_with("hello")
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_inaccessible_message_gets_fresh_reply():
    # Telegram reports messages older than 48h as inaccessible with date = epoch 0.
    epoch = datetime.fromtimestamp(0, timezone.utc)
    chat = SimpleNamespace(send_message=AsyncMock())
    query = SimpleNamespace(
        message=SimpleNamespace(date=epoch, chat=chat), edit_message_text=AsyncMock()
    )
    await _edit_message(query, "hello")
    chat.send_message.assert_awaited_once_with("hello")
    query.edit_message_text.assert_not_awaited()


def test_sync_label_shows_calendar_date(tmp_path):
    tracker = DonationTracker.__new__(DonationTracker)
    tracker.storage = DonationStorage(
        str(tmp_path / "donation_storage.json"), str(tmp_path / "last_season_storage.json")
    )
    tracker.storage.update_last_sync()
    label = tracker._sync_label()
    assert label.startswith("Synced ")
    # The actual date must be in the message text, so nobody has to trust Telegram's bubble date.
    from zoneinfo import ZoneInfo

    from coc_tracker.config import DISPLAY_TZ

    today = datetime.now(ZoneInfo(DISPLAY_TZ))
    assert f"{today.day} {today:%b %Y}" in label
