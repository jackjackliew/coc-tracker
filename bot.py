"""
War Snipers CoC Donation Tracker
=================================
Tracks cumulative donations across all War Snipers clans.
Members can freely jump between any of the 5 clans — all donations add up correctly.

Key guarantees:
  - Every donation is captured regardless of how many clans a member jumps through
  - Season boundaries match CoC exactly (goldpass API), with calendar month as fallback
  - Efficient: single disk write per 10s sync cycle, not per member
"""

import os
import json
import logging
import requests
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

COC_API_BASE = "https://api.clashofclans.com/v1"
STORAGE_FILE = os.path.join(os.path.dirname(__file__), "donation_storage.json")
LAST_SEASON_FILE = os.path.join(os.path.dirname(__file__), "last_season_storage.json")
POLL_INTERVAL = 10           # seconds between background syncs
SEASON_CHECK_INTERVAL = 600  # seconds between CoC season boundary checks (10 min)


def build_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏆 Donations", callback_data="donation"),
            InlineKeyboardButton("📊 Clan List", callback_data="clanlist"),
        ],
        [
            InlineKeyboardButton("📜 Last Season", callback_data="lastseason"),
            InlineKeyboardButton("🔍 Check Tags", callback_data="checktags"),
        ],
    ])


# ─── Storage ──────────────────────────────────────────────────────────────────

class DonationStorage:
    """
    Manages all donation state on disk.

    Schema (donation_storage.json):
    {
      "season_key": "20260401",    ← CoC season start date from goldpass API (YYYYMMDD)
      "clan_tags": ["#TAG1", ...],
      "players": {
        "#PLAYERTAG": {
          "name":           "PlayerName",
          "bonus":          1000,   ← donations locked in from all previous clan visits
          "last_clan":      "#TAG", ← last clan we saw this member in
          "last_donations": 250     ← their current API count in last_clan
        }
      }
    }

    Season total for a member = bonus + last_donations

    Three cases handled on every member update:
      1. Different clan seen        → bonus += last_donations (lock in old clan's count)
      2. Same clan, count DROPPED   → bonus += last_donations (member left and rejoined,
                                      CoC resets their count to 0 on rejoin)
      3. Same clan, count SAME/UP   → normal update, just store the new count
    """

    def __init__(self):
        self.data = self._load()
        self._dirty = False

    def _load(self):
        if os.path.exists(STORAGE_FILE):
            try:
                with open(STORAGE_FILE, "r") as f:
                    data = json.load(f)
                # Migrate old format: "season": "2026-04" → "season_key": "20260401"
                if "season" in data and "season_key" not in data:
                    old = data.pop("season", "")
                    try:
                        year, month = old.split("-")
                        data["season_key"] = f"{year}{month}01"
                    except Exception:
                        data["season_key"] = datetime.now().strftime("%Y%m01")
                    logger.info(f"Storage migrated to season_key: {data['season_key']}")
                return data
            except Exception as e:
                logger.error(f"Failed to load storage, starting fresh: {e}")
        return {"season_key": "", "clan_tags": [], "players": {}}

    def flush(self):
        """Write to disk only when data has actually changed since last flush."""
        if not self._dirty:
            return
        try:
            with open(STORAGE_FILE, "w") as f:
                json.dump(self.data, f, indent=2)
            self._dirty = False
        except Exception as e:
            logger.error(f"Failed to save storage: {e}")

    # ── Season management ──────────────────────────────────────────────────────

    def handle_season_change(self, new_season_key: str):
        """
        Called with the current CoC season key (from goldpass API or fallback).
        If the key has changed, snapshot the old season and start fresh.
        No-op if season key is unchanged — safe to call on every sync.
        """
        if self.data.get("season_key") == new_season_key:
            return  # Same season — nothing to do

        old_key = self.data.get("season_key", "none")
        logger.info(f"Season changed: {old_key} → {new_season_key}. Snapshotting...")

        if self.data.get("players"):
            self._snapshot_to_last_season()

        self.data = {
            "season_key": new_season_key,
            "clan_tags": self.data.get("clan_tags", []),
            "players": {},
        }
        self._dirty = True
        self.flush()  # Immediate flush on season change

    def _snapshot_to_last_season(self):
        """Save final season totals to last_season_storage.json (kept 2 weeks)."""
        snapshot = {
            tag: {
                "name": info.get("name", "Unknown"),
                "total": info.get("bonus", 0) + info.get("last_donations", 0),
            }
            for tag, info in self.data.get("players", {}).items()
        }
        expires_at = (datetime.now() + timedelta(weeks=2)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with open(LAST_SEASON_FILE, "w") as f:
                json.dump({
                    "season_key": self.data.get("season_key", ""),
                    "expires_at": expires_at,
                    "players": snapshot,
                }, f, indent=2)
            logger.info(f"Season snapshot saved, expires {expires_at}")
        except Exception as e:
            logger.error(f"Failed to write season snapshot: {e}")

    # ── Clan tag caching ───────────────────────────────────────────────────────

    def cache_clan_tags(self, clan_tags: list):
        if set(clan_tags) != set(self.data.get("clan_tags", [])):
            self.data["clan_tags"] = list(clan_tags)
            self._dirty = True

    def get_cached_clan_tags(self) -> list:
        return self.data.get("clan_tags", [])

    # ── Player donation updates ────────────────────────────────────────────────

    def update_player(self, player_tag: str, player_name: str,
                      current_donations: int, current_clan_tag: str) -> int:
        """
        Update one player's donation record. Returns their season total.
        Does NOT flush to disk — caller must call flush() after a full batch.
        """
        players = self.data["players"]

        if player_tag not in players:
            # First time we see this player this season
            players[player_tag] = {
                "name": player_name,
                "bonus": 0,
                "last_clan": current_clan_tag,
                "last_donations": current_donations,
            }
            self._dirty = True
        else:
            p = players[player_tag]
            changed = False

            if p.get("name") != player_name:
                p["name"] = player_name
                changed = True

            if p["last_clan"] != current_clan_tag:
                # ── Case 1: Member moved to a different clan ─────────────────
                # Lock their previous clan's count into bonus, start tracking fresh.
                p["bonus"] += p["last_donations"]
                p["last_clan"] = current_clan_tag
                p["last_donations"] = current_donations
                logger.info(
                    f"[CLAN MOVE]  {player_name} → {current_clan_tag} | "
                    f"bonus={p['bonus']}, new count={current_donations}"
                )
                changed = True

            elif current_donations < p["last_donations"]:
                # ── Case 2: Same clan, count dropped ────────────────────────
                # Member left and rejoined the same clan. CoC resets their
                # donation count to 0 on every rejoin, so a lower count
                # on the same clan means they completed a leave-rejoin cycle.
                p["bonus"] += p["last_donations"]
                p["last_donations"] = current_donations
                logger.info(
                    f"[REJOIN]     {player_name} rejoined {current_clan_tag} | "
                    f"bonus={p['bonus']}, new count={current_donations}"
                )
                changed = True

            elif current_donations != p["last_donations"]:
                # ── Case 3: Normal donation increase ────────────────────────
                p["last_donations"] = current_donations
                changed = True

            if changed:
                self._dirty = True

        return players[player_tag]["bonus"] + current_donations

    # ── Last season ────────────────────────────────────────────────────────────

    def get_last_season(self):
        """Returns (players_list, season_label, days_left) or (None, None, None)."""
        if not os.path.exists(LAST_SEASON_FILE):
            return None, None, None
        try:
            with open(LAST_SEASON_FILE, "r") as f:
                data = json.load(f)
            expires_at = datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%S")
            if datetime.now() >= expires_at:
                os.remove(LAST_SEASON_FILE)
                return None, None, None
            players = sorted(
                [{"name": v["name"], "donations": v["total"]} for v in data["players"].values()],
                key=lambda x: x["donations"], reverse=True,
            )
            season_key = data.get("season_key", "")
            try:
                season_label = datetime.strptime(season_key, "%Y%m%d").strftime("%B %Y")
            except Exception:
                season_label = season_key
            days_left = max(0, (expires_at - datetime.now()).days)
            return players, season_label, days_left
        except Exception as e:
            logger.error(f"Error reading last season: {e}")
            return None, None, None

    def cleanup_last_season_if_expired(self):
        self.get_last_season()  # Handles deletion internally when expired


# ─── CoC API ──────────────────────────────────────────────────────────────────

class ClashAPI:
    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _get(self, url: str):
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            logger.warning(f"GET failed [{url}]: {e}")
            return None

    def get_clan_members(self, clan_tag: str):
        return self._get(f"{COC_API_BASE}/clans/{clan_tag.replace('#', '%23')}/members")

    def get_clan_info(self, clan_tag: str):
        return self._get(f"{COC_API_BASE}/clans/{clan_tag.replace('#', '%23')}")

    def get_season_key(self):
        """
        Returns current CoC season start date as "YYYYMMDD" (e.g. "20260401").
        CoC seasons end on the last Monday of the month — NOT the 1st.
        Using the goldpass API prevents us from mis-detecting the CoC season
        reset as members "rejoining" their clan during the calendar-month gap.
        Returns None if API is unavailable.
        """
        data = self._get(f"{COC_API_BASE}/goldpass/seasons/current")
        if data and "startTime" in data:
            try:
                return data["startTime"][:8]  # "20260401T000000.000Z" → "20260401"
            except Exception:
                pass
        return None


# ─── Tracker ──────────────────────────────────────────────────────────────────

class DonationTracker:
    def __init__(self, api_token: str):
        self.api = ClashAPI(api_token)
        self.storage = DonationStorage()
        self._last_season_check = datetime.min  # Force a check on very first sync

    def _refresh_season_if_due(self):
        """
        Check CoC goldpass API every SEASON_CHECK_INTERVAL seconds.
        If the API is down, keep the stored season key to avoid false resets.
        """
        now = datetime.now()
        if (now - self._last_season_check).total_seconds() < SEASON_CHECK_INTERVAL:
            return
        self._last_season_check = now

        season_key = self.api.get_season_key()
        if season_key:
            self.storage.handle_season_change(season_key)
        else:
            # API unavailable — retain stored key; fall back to calendar month only
            # if we have no stored key at all (e.g. fresh install).
            fallback = self.storage.data.get("season_key") or datetime.now().strftime("%Y%m01")
            logger.warning(f"Goldpass API unavailable, retaining season key: {fallback}")
            self.storage.handle_season_change(fallback)

    def parse_clan_tags(self, description: str) -> list:
        tags = re.findall(r"#[A-Z0-9]+", description.upper())
        seen, unique = set(), []
        for tag in tags:
            if tag not in seen:
                seen.add(tag)
                unique.append(tag)
        return unique

    def sync_all_clans(self, clan_tags: list):
        """
        Background sync: poll every clan and update all member records.
        Season check happens once per SEASON_CHECK_INTERVAL, not per member.
        Single disk write per sync cycle (not per member).
        """
        if not clan_tags:
            return

        self._refresh_season_if_due()  # Once per sync, not per member

        seen_players = set()
        for clan_tag in clan_tags:
            data = self.api.get_clan_members(clan_tag)
            if not data or "items" not in data:
                continue
            for member in data["items"]:
                player_tag = member.get("tag", "")
                if player_tag in seen_players:
                    continue
                seen_players.add(player_tag)
                self.storage.update_player(
                    player_tag,
                    member.get("name", "Unknown"),
                    member.get("donations", 0),
                    clan_tag,
                )

        self.storage.flush()  # ONE disk write for the entire sync cycle
        self.storage.cleanup_last_season_if_expired()
        logger.debug(f"Sync complete — {len(seen_players)} players updated.")

    def get_all_donations(self, clan_tags: list) -> list:
        """Fetch fresh data and return global donation leaderboard."""
        self._refresh_season_if_due()
        seen_players = set()
        players = []
        for clan_tag in clan_tags:
            data = self.api.get_clan_members(clan_tag)
            if not data or "items" not in data:
                continue
            for member in data["items"]:
                player_tag = member.get("tag", "")
                if player_tag in seen_players:
                    continue
                seen_players.add(player_tag)
                total = self.storage.update_player(
                    player_tag,
                    member.get("name", "Unknown"),
                    member.get("donations", 0),
                    clan_tag,
                )
                players.append({"name": member.get("name", "Unknown"), "donations": total})
        self.storage.flush()
        players.sort(key=lambda x: x["donations"], reverse=True)
        return players

    def get_all_by_clan(self, clan_tags: list) -> list:
        """
        Fetch fresh data for each clan.
        Returns list of (clan_name, clan_tag, players).
        Single flush after all clans processed.
        """
        self._refresh_season_if_due()
        results = []
        for clan_tag in clan_tags:
            clan_info = self.api.get_clan_info(clan_tag)
            members_data = self.api.get_clan_members(clan_tag)
            if not members_data or "items" not in members_data:
                continue
            clan_name = clan_info.get("name", "Unknown Clan") if clan_info else "Unknown Clan"
            players = []
            for member in members_data["items"]:
                player_tag = member.get("tag", "")
                total = self.storage.update_player(
                    player_tag,
                    member.get("name", "Unknown"),
                    member.get("donations", 0),
                    clan_tag,
                )
                players.append({"name": member.get("name", "Unknown"), "donations": total})
            players.sort(key=lambda x: x["donations"], reverse=True)
            results.append((clan_name, clan_tag, players))
        self.storage.flush()  # One flush after all clans
        return results

    # ── Formatters ─────────────────────────────────────────────────────────────

    def format_leaderboard(self, players: list, title: str = "🏆 Top Donators This Season 🏆") -> str:
        if not players:
            return "❌ No donation data found."
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        ts = datetime.now().strftime("%-m/%-d/%Y, %-I:%M:%S %p")
        lines = [title, ""]
        for i, p in enumerate(players, 1):
            lines.append(f"{medals.get(i, '▫️')} {i}. {p['name']} — {p['donations']:,}")
        lines += ["", f"Last updated: {ts}"]
        return "\n".join(lines)

    def format_by_clan(self, clan_tags: list) -> str:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        ts = datetime.now().strftime("%-m/%-d/%Y, %-I:%M:%S %p")
        lines = ["📊 Donations by Clan 📊", ""]
        for clan_name, clan_tag, players in self.get_all_by_clan(clan_tags):
            lines.append(f"🏰 {clan_name} ({clan_tag})")
            for i, p in enumerate(players, 1):
                lines.append(f"  {medals.get(i, '▫️')} {p['name']} — {p['donations']:,}")
            lines.append("")
        lines.append(f"Last updated: {ts}")
        return "\n".join(lines)


# ─── Background Sync ──────────────────────────────────────────────────────────

async def background_sync(context):
    clan_tags = tracker.storage.get_cached_clan_tags()
    if not clan_tags:
        logger.debug("Background sync skipped — no clan tags cached yet.")
        return
    tracker.sync_all_clans(clan_tags)


# ─── Bot Wiring ───────────────────────────────────────────────────────────────

tracker = None


def group_only(func):
    """Restrict a command handler to group/supergroup chats."""
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


async def get_clan_tags_from_chat(update, context) -> list:
    chat = await context.bot.get_chat(update.effective_chat.id)
    return tracker.parse_clan_tags(chat.description or "")


async def _send_chunks(target, text: str, reply_markup=None):
    """Send text, splitting at 4096 chars. Keyboard attached to last chunk only."""
    chunks = [text[i:i + 4096] for i in range(0, len(text), 4096)]
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        await target.send_message(chunk, reply_markup=markup)


# ─── Command Handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *War Snipers Donation Tracker*\n\nTap a button to get started:",
        reply_markup=build_menu_keyboard(),
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.callback_query.edit_message_text(
            text, reply_markup=build_menu_keyboard(), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(text, reply_markup=build_menu_keyboard(), parse_mode="Markdown")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Choose a command:", reply_markup=build_menu_keyboard())


@group_only
async def donation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_cb = bool(update.callback_query)
    if is_cb:
        await update.callback_query.answer("⏳ Fetching...")
    else:
        await update.message.reply_text("⏳ Fetching donation data...")
    try:
        clan_tags = await get_clan_tags_from_chat(update, context)
        if not clan_tags:
            msg = "❌ No clan tags found in group description."
            if is_cb:
                await update.callback_query.edit_message_text(msg, reply_markup=build_menu_keyboard())
            else:
                await update.message.reply_text(msg)
            return
        tracker.storage.cache_clan_tags(clan_tags)
        players = tracker.get_all_donations(clan_tags)
        msg = tracker.format_leaderboard(players)
        if is_cb:
            await update.callback_query.edit_message_text(msg[:4096], reply_markup=build_menu_keyboard())
            if len(msg) > 4096:
                await _send_chunks(update.effective_chat, msg[4096:])
        else:
            await _send_chunks(update.effective_chat, msg, reply_markup=build_menu_keyboard())
    except Exception as e:
        logger.error(f"/donation error: {e}")
        err = f"❌ Error: {e}"
        if is_cb:
            await update.callback_query.edit_message_text(err, reply_markup=build_menu_keyboard())
        else:
            await update.message.reply_text(err)


@group_only
async def clanlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_cb = bool(update.callback_query)
    if is_cb:
        await update.callback_query.answer("⏳ Fetching...")
    else:
        await update.message.reply_text("⏳ Fetching clan data...")
    try:
        clan_tags = await get_clan_tags_from_chat(update, context)
        if not clan_tags:
            msg = "❌ No clan tags found in group description."
            if is_cb:
                await update.callback_query.edit_message_text(msg, reply_markup=build_menu_keyboard())
            else:
                await update.message.reply_text(msg)
            return
        tracker.storage.cache_clan_tags(clan_tags)
        msg = tracker.format_by_clan(clan_tags)
        if is_cb:
            await update.callback_query.edit_message_text(msg[:4096], reply_markup=build_menu_keyboard())
            if len(msg) > 4096:
                await _send_chunks(update.effective_chat, msg[4096:])
        else:
            await _send_chunks(update.effective_chat, msg, reply_markup=build_menu_keyboard())
    except Exception as e:
        logger.error(f"/clanlist error: {e}")
        err = f"❌ Error: {e}"
        if is_cb:
            await update.callback_query.edit_message_text(err, reply_markup=build_menu_keyboard())
        else:
            await update.message.reply_text(err)


@group_only
async def lastseason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_cb = bool(update.callback_query)
    if is_cb:
        await update.callback_query.answer()
    try:
        players, season_label, days_left = tracker.storage.get_last_season()
        if players is None:
            msg = "❌ No last season data available.\nRecords are kept for 2 weeks after season ends."
            if is_cb:
                await update.callback_query.edit_message_text(msg, reply_markup=build_menu_keyboard())
            else:
                await update.message.reply_text(msg)
            return
        title = f"📜 Last Season ({season_label}) Final Donations 📜\n⏳ Expires in {days_left} day(s)"
        msg = tracker.format_leaderboard(players, title=title)
        if is_cb:
            await update.callback_query.edit_message_text(msg[:4096], reply_markup=build_menu_keyboard())
        else:
            await _send_chunks(update.effective_chat, msg, reply_markup=build_menu_keyboard())
    except Exception as e:
        logger.error(f"/lastseason error: {e}")
        err = f"❌ Error: {e}"
        if is_cb:
            await update.callback_query.edit_message_text(err, reply_markup=build_menu_keyboard())
        else:
            await update.message.reply_text(err)


@group_only
async def checktags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_cb = bool(update.callback_query)
    if is_cb:
        await update.callback_query.answer("🔍 Checking...")
    try:
        clan_tags = await get_clan_tags_from_chat(update, context)
        if not clan_tags:
            msg = "❌ No clan tags found in group description."
            if is_cb:
                await update.callback_query.edit_message_text(msg, reply_markup=build_menu_keyboard())
            else:
                await update.message.reply_text(msg)
            return
        lines = ["📋 *Detected Clan Tags:*", ""]
        for tag in clan_tags:
            info = tracker.api.get_clan_info(tag)
            name = info.get("name", "Unknown") if info else "❌ Unreachable"
            lines.append(f"▫️ {tag} — {name}")
        text = "\n".join(lines)
        if is_cb:
            await update.callback_query.edit_message_text(
                text, reply_markup=build_menu_keyboard(), parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"/checktags error: {e}")
        err = f"❌ Error: {e}"
        if is_cb:
            await update.callback_query.edit_message_text(err, reply_markup=build_menu_keyboard())
        else:
            await update.message.reply_text(err)


# ─── Callback Router ──────────────────────────────────────────────────────────

CALLBACK_MAP = {
    "donation":   donation,
    "clanlist":   clanlist,
    "lastseason": lastseason,
    "checktags":  checktags,
}


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    handler = CALLBACK_MAP.get(update.callback_query.data)
    if handler:
        await handler(update, context)
    else:
        await update.callback_query.answer("Unknown action.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    coc_api_token = os.getenv("COC_API_TOKEN")
    if not telegram_token or not coc_api_token:
        logger.error("Missing TELEGRAM_TOKEN or COC_API_TOKEN environment variables.")
        return

    global tracker
    tracker = DonationTracker(coc_api_token)

    app = Application.builder().token(telegram_token).build()

    for cmd, fn in [
        ("start",      start),
        ("help",       help_cmd),
        ("menu",       menu),
        ("donation",   donation),
        ("clanlist",   clanlist),
        ("lastseason", lastseason),
        ("checktags",  checktags),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(background_sync, interval=POLL_INTERVAL, first=5)
    logger.info(f"Bot started. Syncing every {POLL_INTERVAL}s.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
