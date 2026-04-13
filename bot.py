"""
War Snipers CoC Donation Tracker
=================================
Tracks cumulative donations across all War Snipers clans.
Members can freely jump between any of the 5 clans — all donations add up correctly.

Performance design:
  - Background sync keeps storage fresh every 10s
  - Button presses serve from in-memory cache (instant, no API calls)
  - Clan names refreshed once per hour (rarely change)
  - Fresh API calls only happen when storage is empty (first run)
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
POLL_INTERVAL = 10            # seconds between background syncs
SEASON_CHECK_INTERVAL = 600   # seconds between CoC season checks (10 min)
CLAN_NAME_REFRESH = 3600      # seconds between clan name refreshes (1 hour)


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
      "season_key":  "20260401",
      "clan_tags":   ["#TAG1", ...],
      "clan_names":  {"#TAG1": "War Snipers", ...},   ← cached, refreshed hourly
      "last_sync":   "2026-04-13T08:58:23",           ← timestamp of last background sync
      "players": {
        "#PLAYERTAG": {
          "name": "PlayerName", "bonus": 1000,
          "last_clan": "#TAG1", "last_donations": 250
        }
      }
    }
    """

    def __init__(self):
        self.data = self._load()
        self._dirty = False

    def _load(self):
        if os.path.exists(STORAGE_FILE):
            try:
                with open(STORAGE_FILE, "r") as f:
                    data = json.load(f)
                # Migrate old "season" key to "season_key"
                if "season" in data and "season_key" not in data:
                    data.pop("season")
                    data["season_key"] = ""
                    logger.info("Storage migrated: season_key will be set from API on first sync")
                return data
            except Exception as e:
                logger.error(f"Failed to load storage, starting fresh: {e}")
        return {"season_key": "", "clan_tags": [], "clan_names": {}, "last_sync": "", "players": {}}

    def flush(self):
        """Write to disk only when data has actually changed."""
        if not self._dirty:
            return
        try:
            with open(STORAGE_FILE, "w") as f:
                json.dump(self.data, f, indent=2)
            self._dirty = False
        except Exception as e:
            logger.error(f"Failed to save storage: {e}")

    # ── Season ─────────────────────────────────────────────────────────────────

    def handle_season_change(self, new_season_key: str):
        stored_key = self.data.get("season_key", "")
        if stored_key == new_season_key:
            return
        if not stored_key:
            logger.info(f"Season key initialized: {new_season_key}")
            self.data["season_key"] = new_season_key
            self._dirty = True
            self.flush()
            return
        logger.info(f"Season changed: {stored_key} → {new_season_key}. Snapshotting...")
        if self.data.get("players"):
            self._snapshot_to_last_season()
        self.data = {
            "season_key": new_season_key,
            "clan_tags": self.data.get("clan_tags", []),
            "clan_names": self.data.get("clan_names", {}),
            "last_sync": "",
            "players": {},
        }
        self._dirty = True
        self.flush()

    def _snapshot_to_last_season(self):
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

    # ── Clan tags & names ──────────────────────────────────────────────────────

    def cache_clan_tags(self, clan_tags: list):
        if set(clan_tags) != set(self.data.get("clan_tags", [])):
            self.data["clan_tags"] = list(clan_tags)
            self._dirty = True

    def get_cached_clan_tags(self) -> list:
        return self.data.get("clan_tags", [])

    def cache_clan_name(self, clan_tag: str, name: str):
        if "clan_names" not in self.data:
            self.data["clan_names"] = {}
        if self.data["clan_names"].get(clan_tag) != name:
            self.data["clan_names"][clan_tag] = name
            self._dirty = True

    def get_clan_name(self, clan_tag: str) -> str:
        return self.data.get("clan_names", {}).get(clan_tag, clan_tag)

    # ── Last sync timestamp ────────────────────────────────────────────────────

    def update_last_sync(self):
        self.data["last_sync"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._dirty = True

    def get_last_sync(self) -> str:
        return self.data.get("last_sync", "")

    # ── Player updates ─────────────────────────────────────────────────────────

    def update_player(self, player_tag: str, player_name: str,
                      current_donations: int, current_clan_tag: str) -> int:
        """
        Update one player's donation record. Returns their season total.
        Does NOT flush — caller must call flush() after a full batch.
        """
        players = self.data["players"]

        if player_tag not in players:
            players[player_tag] = {
                "name": player_name, "bonus": 0,
                "last_clan": current_clan_tag, "last_donations": current_donations,
            }
            self._dirty = True
        else:
            p = players[player_tag]
            changed = False

            if p.get("name") != player_name:
                p["name"] = player_name
                changed = True

            if p["last_clan"] != current_clan_tag:
                # Player moved to a different clan — lock in previous donations
                p["bonus"] += p["last_donations"]
                p["last_clan"] = current_clan_tag
                p["last_donations"] = current_donations
                logger.info(f"[MOVE]   {player_name} → {current_clan_tag} | bonus={p['bonus']}")
                changed = True
            elif current_donations < p["last_donations"]:
                # Same clan, count dropped — left and rejoined (CoC resets to 0)
                p["bonus"] += p["last_donations"]
                p["last_donations"] = current_donations
                logger.info(f"[REJOIN] {player_name} back to {current_clan_tag} | bonus={p['bonus']}")
                changed = True
            elif current_donations != p["last_donations"]:
                p["last_donations"] = current_donations
                changed = True

            if changed:
                self._dirty = True

        return players[player_tag]["bonus"] + current_donations

    # ── Fast reads from cache (no API calls) ──────────────────────────────────

    def get_all_players_sorted(self) -> list:
        """Return all players sorted by total donations. Reads from memory — instant."""
        players = self.data.get("players", {})
        result = [
            {"name": p["name"], "donations": p["bonus"] + p["last_donations"]}
            for p in players.values()
        ]
        result.sort(key=lambda x: x["donations"], reverse=True)
        return result

    def get_players_by_clan(self, clan_tags: list) -> list:
        """
        Return players grouped by their current clan, ordered by clan_tags.
        Reads from memory — instant. No API calls.
        """
        players = self.data.get("players", {})
        clan_names = self.data.get("clan_names", {})

        # Group players by last known clan
        by_clan: dict = {}
        for p in players.values():
            clan = p.get("last_clan", "")
            if clan not in by_clan:
                by_clan[clan] = []
            by_clan[clan].append({
                "name": p["name"],
                "donations": p["bonus"] + p["last_donations"],
            })

        results = []
        for clan_tag in clan_tags:
            if clan_tag in by_clan:
                clan_players = sorted(by_clan[clan_tag], key=lambda x: x["donations"], reverse=True)
                clan_name = clan_names.get(clan_tag, clan_tag)
                results.append((clan_name, clan_tag, clan_players))
        return results

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
            season_key = data.get("season_key") or data.get("season", "")
            try:
                season_label = datetime.strptime(season_key, "%Y%m%d").strftime("%B %Y")
            except Exception:
                try:
                    season_label = datetime.strptime(season_key, "%Y-%m").strftime("%B %Y")
                except Exception:
                    season_label = season_key or "Unknown"
            days_left = max(0, (expires_at - datetime.now()).days)
            return players, season_label, days_left
        except Exception as e:
            logger.error(f"Error reading last season: {e}")
            return None, None, None

    def cleanup_last_season_if_expired(self):
        self.get_last_season()


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
        data = self._get(f"{COC_API_BASE}/goldpass/seasons/current")
        if data and "startTime" in data:
            try:
                return data["startTime"][:8]
            except Exception:
                pass
        return None


# ─── Tracker ──────────────────────────────────────────────────────────────────

class DonationTracker:
    def __init__(self, api_token: str):
        self.api = ClashAPI(api_token)
        self.storage = DonationStorage()
        self._last_season_check = datetime.min
        self._last_clan_name_refresh = datetime.min

    def _refresh_season_if_due(self):
        now = datetime.now()
        if (now - self._last_season_check).total_seconds() < SEASON_CHECK_INTERVAL:
            return
        self._last_season_check = now
        season_key = self.api.get_season_key()
        if season_key:
            self.storage.handle_season_change(season_key)
        else:
            fallback = self.storage.data.get("season_key") or datetime.now().strftime("%Y%m01")
            logger.debug(f"Goldpass API unavailable (non-critical), using key: {fallback}")
            self.storage.handle_season_change(fallback)

    def _refresh_clan_names_if_due(self, clan_tags: list):
        """Refresh clan names from API once per hour — they rarely change."""
        now = datetime.now()
        if (now - self._last_clan_name_refresh).total_seconds() < CLAN_NAME_REFRESH:
            return
        self._last_clan_name_refresh = now
        for clan_tag in clan_tags:
            info = self.api.get_clan_info(clan_tag)
            if info and "name" in info:
                self.storage.cache_clan_name(clan_tag, info["name"])
        logger.debug(f"Clan names refreshed for {len(clan_tags)} clans")

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
        Background sync: poll every clan, update all member records.
        Single disk write per cycle. Refreshes clan names hourly.
        """
        if not clan_tags:
            return

        self._refresh_season_if_due()
        self._refresh_clan_names_if_due(clan_tags)

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

        self.storage.update_last_sync()
        self.storage.flush()
        self.storage.cleanup_last_season_if_expired()
        logger.debug(f"Sync complete — {len(seen_players)} players updated.")

    def _sync_label(self) -> str:
        """Human-readable 'synced X seconds ago' label from stored timestamp."""
        last_sync = self.storage.get_last_sync()
        if not last_sync:
            return datetime.now().strftime("%-m/%-d/%Y, %-I:%M:%S %p")
        try:
            sync_dt = datetime.strptime(last_sync, "%Y-%m-%dT%H:%M:%S")
            secs = int((datetime.now() - sync_dt).total_seconds())
            if secs < 60:
                return f"Synced {secs}s ago"
            return f"Synced {secs // 60}m ago"
        except Exception:
            return last_sync

    def format_leaderboard(self, players: list, title: str = "🏆 Top Donators This Season 🏆") -> str:
        if not players:
            return "❌ No donation data yet. Sync runs every 10s — try again shortly."
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [title, ""]
        for i, p in enumerate(players, 1):
            lines.append(f"{medals.get(i, '▫️')} {i}. {p['name']} — {p['donations']:,}")
        lines += ["", f"📡 {self._sync_label()}"]
        return "\n".join(lines)

    def format_by_clan(self, clan_tags: list) -> str:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = ["📊 Donations by Clan 📊", ""]
        for clan_name, clan_tag, players in self.storage.get_players_by_clan(clan_tags):
            lines.append(f"🏰 {clan_name} ({clan_tag})")
            for i, p in enumerate(players, 1):
                lines.append(f"  {medals.get(i, '▫️')} {p['name']} — {p['donations']:,}")
            lines.append("")
        lines.append(f"📡 {self._sync_label()}")
        return "\n".join(lines)

    def get_all_donations_fresh(self, clan_tags: list) -> list:
        """Force fresh API call — used only when cache is empty (first run)."""
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
        self.storage.update_last_sync()
        self.storage.flush()
        players.sort(key=lambda x: x["donations"], reverse=True)
        return players


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
        await update.callback_query.answer()
    else:
        await update.message.reply_text("⏳ Loading...")

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

        # Serve from cache (instant). Fall back to fresh API only if no data yet.
        players = tracker.storage.get_all_players_sorted()
        if not players:
            players = tracker.get_all_donations_fresh(clan_tags)

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
        await update.callback_query.answer()
    else:
        await update.message.reply_text("⏳ Loading...")

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

        # If no clan names cached yet, fetch them first (only on very first run)
        clan_names = tracker.storage.data.get("clan_names", {})
        if not any(t in clan_names for t in clan_tags):
            for tag in clan_tags:
                info = tracker.api.get_clan_info(tag)
                if info and "name" in info:
                    tracker.storage.cache_clan_name(tag, info["name"])
            tracker._last_clan_name_refresh = datetime.now()
            tracker.storage.flush()

        msg = tracker.format_by_clan(clan_tags)

        if not tracker.storage.get_all_players_sorted():
            msg = "⏳ No sync data yet — please wait up to 10 seconds and try again."

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
