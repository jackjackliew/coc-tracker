"""Telegram command + callback handlers."""

from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .config import POLL_INTERVAL, TELEGRAM_MESSAGE_LIMIT
from .keyboard import build_menu_keyboard
from .tracker import DonationTracker

logger = logging.getLogger(__name__)

# Set by main() before handlers run
tracker: DonationTracker | None = None


def set_tracker(t: DonationTracker) -> None:
    global tracker
    tracker = t


# ─── Helpers ──────────────────────────────────────────────────────────────────


def group_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_type = update.effective_chat.type if update.effective_chat else None
        if chat_type not in ["group", "supergroup"]:
            msg = "❌ This command only works in the War Snipers group."
            if update.callback_query:
                await update.callback_query.answer(msg, show_alert=True)
            elif update.message:
                await update.message.reply_text(msg)
            return
        await func(update, context)

    return wrapper


async def get_clan_tags_from_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> list:
    chat = await context.bot.get_chat(update.effective_chat.id)
    return DonationTracker.parse_clan_tags(chat.description or "")


async def _send_chunks(target, text: str, reply_markup=None) -> None:
    chunks = [
        text[i : i + TELEGRAM_MESSAGE_LIMIT] for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)
    ]
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        await target.send_message(chunk, reply_markup=markup)


async def _edit_message(query, text: str, reply_markup=None, parse_mode=None) -> None:
    """Edit a callback message. Silently ignores Telegram's 'not modified' error."""
    try:
        kwargs = {}
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            pass  # Content unchanged — silently ignore
        else:
            raise


# ─── Background Sync ──────────────────────────────────────────────────────────


async def background_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    if tracker is None:
        return
    clan_tags = tracker.storage.get_cached_clan_tags()
    if not clan_tags:
        logger.debug("Background sync skipped — no clan tags cached yet.")
        return
    await tracker.sync_all_clans(clan_tags)


# ─── Command Handlers ─────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *War Snipers Donation Tracker*\n\nTap a button to get started:",
        reply_markup=build_menu_keyboard(),
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 *Commands:*\n\n"
        "🏆 /donation — Combined leaderboard across all clans\n"
        "📊 /clanlist — Donations grouped by each clan\n"
        "📜 /lastseason — Last season's records (kept 2 weeks)\n"
        "🔍 /checktags — Verify which clans I'm tracking\n"
        "📋 /menu — Show button menu\n\n"
        f"ℹ️ Syncs every {POLL_INTERVAL}s. Jumping between clans does NOT reset your count."
    )
    if update.callback_query:
        await update.callback_query.answer()
        await _edit_message(
            update.callback_query, text, reply_markup=build_menu_keyboard(), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=build_menu_keyboard(), parse_mode="Markdown"
        )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📋 Choose a command:", reply_markup=build_menu_keyboard())


@group_only
async def donation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_cb = bool(update.callback_query)
    if is_cb:
        await update.callback_query.answer()
    else:
        await update.message.reply_text("⏳ Loading...")

    try:
        clan_tags = await get_clan_tags_from_chat(update, context)
        if not clan_tags:
            msg = "❌ No clan tags found in group description."
            if is_cb:
                await update.callback_query.edit_message_text(
                    msg, reply_markup=build_menu_keyboard()
                )
            else:
                await update.message.reply_text(msg)
            return

        tracker.storage.cache_clan_tags(clan_tags)

        # Serve from cache (instant). Fall back to fresh API only if no data yet.
        players = tracker.storage.get_all_players_sorted()
        if not players:
            players = await tracker.get_all_donations_fresh(clan_tags)

        msg = tracker.format_leaderboard(players)

        if is_cb:
            await _edit_message(
                update.callback_query,
                msg[:TELEGRAM_MESSAGE_LIMIT],
                reply_markup=build_menu_keyboard(),
            )
            if len(msg) > TELEGRAM_MESSAGE_LIMIT:
                await _send_chunks(update.effective_chat, msg[TELEGRAM_MESSAGE_LIMIT:])
        else:
            await _send_chunks(update.effective_chat, msg, reply_markup=build_menu_keyboard())

    except Exception as e:
        logger.error(f"/donation error: {e}")
        err = f"❌ Error: {e}"
        if is_cb:
            await _edit_message(update.callback_query, err, reply_markup=build_menu_keyboard())
        else:
            await update.message.reply_text(err)


@group_only
async def clanlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_cb = bool(update.callback_query)
    if is_cb:
        await update.callback_query.answer()
    else:
        await update.message.reply_text("⏳ Loading...")

    try:
        clan_tags = await get_clan_tags_from_chat(update, context)
        if not clan_tags:
            msg = "❌ No clan tags found in group description."
            if is_cb:
                await update.callback_query.edit_message_text(
                    msg, reply_markup=build_menu_keyboard()
                )
            else:
                await update.message.reply_text(msg)
            return

        tracker.storage.cache_clan_tags(clan_tags)

        # If no clan names cached yet, fetch them first (only on very first run)
        clan_names = tracker.storage.data.get("clan_names", {})
        if not any(t in clan_names for t in clan_tags):
            for tag in clan_tags:
                info = await tracker.api.get_clan_info(tag)
                if info and "name" in info:
                    tracker.storage.cache_clan_name(tag, info["name"])
            tracker._last_clan_name_refresh = datetime.now()
            tracker.storage.flush()

        msg = tracker.format_by_clan(clan_tags)

        if not tracker.storage.get_all_players_sorted():
            msg = "⏳ No sync data yet — please wait up to 10 seconds and try again."

        if is_cb:
            await _edit_message(
                update.callback_query,
                msg[:TELEGRAM_MESSAGE_LIMIT],
                reply_markup=build_menu_keyboard(),
            )
            if len(msg) > TELEGRAM_MESSAGE_LIMIT:
                await _send_chunks(update.effective_chat, msg[TELEGRAM_MESSAGE_LIMIT:])
        else:
            await _send_chunks(update.effective_chat, msg, reply_markup=build_menu_keyboard())

    except Exception as e:
        logger.error(f"/clanlist error: {e}")
        err = f"❌ Error: {e}"
        if is_cb:
            await _edit_message(update.callback_query, err, reply_markup=build_menu_keyboard())
        else:
            await update.message.reply_text(err)


@group_only
async def lastseason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_cb = bool(update.callback_query)
    if is_cb:
        await update.callback_query.answer()

    try:
        players, season_label, days_left = tracker.storage.get_last_season()
        if players is None:
            msg = (
                "❌ No last season data available.\nRecords are kept for 2 weeks after season ends."
            )
            if is_cb:
                await update.callback_query.edit_message_text(
                    msg, reply_markup=build_menu_keyboard()
                )
            else:
                await update.message.reply_text(msg)
            return

        title = (
            f"📜 Last Season ({season_label}) Final Donations 📜\n⏳ Expires in {days_left} day(s)"
        )
        msg = tracker.format_leaderboard(players, title=title)

        if is_cb:
            await _edit_message(
                update.callback_query,
                msg[:TELEGRAM_MESSAGE_LIMIT],
                reply_markup=build_menu_keyboard(),
            )
        else:
            await _send_chunks(update.effective_chat, msg, reply_markup=build_menu_keyboard())

    except Exception as e:
        logger.error(f"/lastseason error: {e}")
        err = f"❌ Error: {e}"
        if is_cb:
            await _edit_message(update.callback_query, err, reply_markup=build_menu_keyboard())
        else:
            await update.message.reply_text(err)


@group_only
async def checktags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_cb = bool(update.callback_query)
    if is_cb:
        await update.callback_query.answer("🔍 Checking...")

    try:
        clan_tags = await get_clan_tags_from_chat(update, context)
        if not clan_tags:
            msg = "❌ No clan tags found in group description."
            if is_cb:
                await update.callback_query.edit_message_text(
                    msg, reply_markup=build_menu_keyboard()
                )
            else:
                await update.message.reply_text(msg)
            return

        lines = ["📋 *Detected Clan Tags:*", ""]
        for tag in clan_tags:
            info = await tracker.api.get_clan_info(tag)
            name = info.get("name", "Unknown") if info else "❌ Unreachable"
            lines.append(f"▫️ {tag} — {name}")
        text = "\n".join(lines)

        if is_cb:
            await _edit_message(
                update.callback_query,
                text,
                reply_markup=build_menu_keyboard(),
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(text, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"/checktags error: {e}")
        err = f"❌ Error: {e}"
        if is_cb:
            await _edit_message(update.callback_query, err, reply_markup=build_menu_keyboard())
        else:
            await update.message.reply_text(err)


# ─── Callback Router ──────────────────────────────────────────────────────────

CALLBACK_MAP = {
    "donation": donation,
    "clanlist": clanlist,
    "lastseason": lastseason,
    "checktags": checktags,
}


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handler = CALLBACK_MAP.get(update.callback_query.data)
    if handler:
        await handler(update, context)
    else:
        await update.callback_query.answer("Unknown action.")
