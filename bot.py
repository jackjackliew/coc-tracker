import os
import json
import logging
import requests
import re
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

COC_API_BASE = "https://api.clashofclans.com/v1"
STORAGE_FILE = os.path.join(os.path.dirname(__file__), "donation_storage.json")
LAST_SEASON_FILE = os.path.join(os.path.dirname(__file__), "last_season_storage.json")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_current_season():
    now = datetime.now()
    return f"{now.year}-{now.month:02d}"


# ─── Storage ──────────────────────────────────────────────────────────────────

class DonationStorage:
    """
    Current-season storage schema:
    {
      "season": "2026-04",
      "clan_tags": ["#TAG1", "#TAG2", ...],
      "players": {
        "#PLAYERTAG": {
          "name": "PlayerName",
          "bonus": 1234,
          "last_clan": "#CLANTAG",
          "last_donations": 567
        }
      }
    }

    Last-season storage schema (last_season_storage.json):
    {
      "season": "2026-03",
      "expires_at": "2026-04-14T00:00:00",
      "players": {
        "#PLAYERTAG": {
          "name": "PlayerName",
          "total": 1234
        }
      }
    }
    """

    def __init__(self):
        self.data = self._load()

    def _load(self):
        if os.path.exists(STORAGE_FILE):
            try:
                with open(STORAGE_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"season": get_current_season(), "clan_tags": [], "players": {}}

    def _save(self):
        try:
            with open(STORAGE_FILE, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save storage: {e}")

    def _snapshot_to_last_season(self):
        """Save current season final totals to last_season_storage.json with 2-week expiry."""
        players = self.data.get("players", {})
        snapshot = {}
        for tag, info in players.items():
            total = info.get("bonus", 0) + info.get("last_donations", 0)
            snapshot[tag] = {"name": info.get("name", "Unknown"), "total": total}

        expires_at = (datetime.now() + timedelta(weeks=2)).strftime("%Y-%m-%dT%H:%M:%S")
        last_season_data = {
            "season": self.data.get("season", "unknown"),
            "expires_at": expires_at,
            "players": snapshot,
        }
        try:
            with open(LAST_SEASON_FILE, "w") as f:
                json.dump(last_season_data, f, indent=2)
            logger.info(f"Snapshotted last season {self.data['season']} — expires {expires_at}")
        except Exception as e:
            logger.error(f"Failed to save last season: {e}")

    def _check_season_reset(self):
        """Detect new season, snapshot old data, then reset."""
        current = get_current_season()
        if self.data.get("season") != current:
            logger.info(f"New season detected: {current}. Snapshotting old season...")
            self._snapshot_to_last_season()
            cached_tags = self.data.get("clan_tags", [])
            self.data = {"season": current, "clan_tags": cached_tags, "players": {}}
            self._save()

    def cache_clan_tags(self, clan_tags):
        """Store clan tags so background sync knows which clans to poll."""
        if set(clan_tags) != set(self.data.get("clan_tags", [])):
            self.data["clan_tags"] = clan_tags
            self._save()

    def get_cached_clan_tags(self):
        return self.data.get("clan_tags", [])

    def update_and_get_total(self, player_tag, player_name, current_donations, current_clan_tag):
        """Update player record and return their season total."""
        self._check_season_reset()
        players = self.data["players"]

        if player_tag not in players:
            players[player_tag] = {
                "name": player_name,
                "bonus": 0,
                "last_clan": current_clan_tag,
                "last_donations": current_donations,
            }
        else:
            stored = players[player_tag]
            stored["name"] = player_name

            if stored["last_clan"] != current_clan_tag:
                # Player moved clan — carry over previous donations as bonus
                stored["bonus"] += stored["last_donations"]
                stored["last_clan"] = current_clan_tag
                stored["last_donations"] = current_donations
                logger.info(
                    f"{player_name} ({player_tag}) moved clan. "
                    f"Bonus now {stored['bonus']}, current {current_donations}"
                )
            else:
                stored["last_donations"] = current_donations

        self._save()
        return players[player_tag]["bonus"] + current_donations

    def silent_update(self, player_tag, player_name, current_donations, current_clan_tag):
        """Background sync version — updates storage quietly without returning total."""
        self.update_and_get_total(player_tag, player_name, current_donations, current_clan_tag)

    def cleanup_last_season_if_expired(self):
        """Auto-delete last season file if 2-week window has passed."""
        if not os.path.exists(LAST_SEASON_FILE):
            return
        try:
            with open(LAST_SEASON_FILE, "r") as f:
                data = json.load(f)
            expires_at = datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%S")
            if datetime.now() >= expires_at:
                os.remove(LAST_SEASON_FILE)
                logger.info("Last season storage expired and removed.")
        except Exception as e:
            logger.error(f"Error during last season cleanup: {e}")


# ─── CoC API ──────────────────────────────────────────────────────────────────

class ClashAPI:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get_clan_members(self, clan_tag):
        encoded_tag = clan_tag.replace("#", "%23")
        url = f"{COC_API_BASE}/clans/{encoded_tag}/members"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            logger.error(f"API Error {response.status_code} for {clan_tag}")
            return None
        except Exception as e:
            logger.error(f"Request failed for {clan_tag}: {e}")
            return None

    def get_clan_info(self, clan_tag):
        encoded_tag = clan_tag.replace("#", "%23")
        url = f"{COC_API_BASE}/clans/{encoded_tag}"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            logger.error(f"Error fetching clan info: {e}")
            return None


# ─── Tracker ──────────────────────────────────────────────────────────────────

class DonationTracker:
    def __init__(self, api_token):
        self.api = ClashAPI(api_token)
        self.storage = DonationStorage()

    def parse_clan_tags(self, description):
        pattern = r'#[A-Z0-9]+'
        tags = re.findall(pattern, description.upper())
        seen = set()
        unique_tags = []
        for tag in tags:
            if tag not in seen:
                seen.add(tag)
                unique_tags.append(tag)
        return unique_tags

    def sync_all_clans(self, clan_tags):
        """Poll all clans and update storage silently. Used by background job."""
        if not clan_tags:
            return
        seen_tags = set()
        for clan_tag in clan_tags:
            data = self.api.get_clan_members(clan_tag)
            if not data or "items" not in data:
                continue
            for member in data["items"]:
                player_tag = member.get("tag", "")
                if player_tag in seen_tags:
                    continue
                seen_tags.add(player_tag)
                self.storage.silent_update(
                    player_tag,
                    member.get("name", "Unknown"),
                    member.get("donations", 0),
                    clan_tag,
                )
        self.storage.cleanup_last_season_if_expired()
        logger.info(f"Background sync complete — {len(seen_tags)} players updated.")

    def get_all_donations(self, clan_tags):
        seen_tags = set()
        all_players = []
        for clan_tag in clan_tags:
            data = self.api.get_clan_members(clan_tag)
            if not data or "items" not in data:
                continue
            for member in data["items"]:
                player_tag = member.get("tag", "")
                if player_tag in seen_tags:
                    continue
                seen_tags.add(player_tag)
                total = self.storage.update_and_get_total(
                    player_tag,
                    member.get("name", "Unknown"),
                    member.get("donations", 0),
                    clan_tag,
                )
                all_players.append({"name": member.get("name", "Unknown"), "donations": total})
        all_players.sort(key=lambda x: x["donations"], reverse=True)
        return all_players

    def get_clan_donations(self, clan_tag):
        clan_info = self.api.get_clan_info(clan_tag)
        members_data = self.api.get_clan_members(clan_tag)
        if not members_data or "items" not in members_data:
            return None, None
        clan_name = clan_info.get("name", "Unknown Clan") if clan_info else "Unknown Clan"
        players = []
        for member in members_data["items"]:
            player_tag = member.get("tag", "")
            total = self.storage.update_and_get_total(
                player_tag,
                member.get("name", "Unknown"),
                member.get("donations", 0),
                clan_tag,
            )
            players.append({"name": member.get("name", "Unknown"), "donations": total})
        players.sort(key=lambda x: x["donations"], reverse=True)
        return players, clan_name

    def format_leaderboard(self, players, title="🏆 Top Donators This Season 🏆"):
        if not players:
            return "❌ No donation data found."
        timestamp = datetime.now().strftime("%-m/%-d/%Y, %-I:%M:%S %p")
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [title, ""]
        for idx, player in enumerate(players, 1):
            medal = medals.get(idx, "▫️")
            lines.append(f"{medal} {idx}. {player['name']} - {player['donations']:,}")
        lines.append("")
        lines.append(f"Last updated: {timestamp}")
        return "\n".join(lines)

    def format_by_clan(self, clan_tags):
        lines = ["📊 Donations by Clan 📊", ""]
        timestamp = datetime.now().strftime("%-m/%-d/%Y, %-I:%M:%S %p")
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for tag in clan_tags:
            players, _ = self.get_clan_donations(tag)
            if players:
                lines.append(f"{tag}")
                for idx, player in enumerate(players, 1):
                    medal = medals.get(idx, "▫️")
                    lines.append(f"{medal} {player['name']} - {player['donations']:,}")
                lines.append("")
        lines.append(f"Last updated: {timestamp}")
        return "\n".join(lines)

    def get_last_season_leaderboard(self):
        """Read last_season_storage.json and return players, season name, days left."""
        if not os.path.exists(LAST_SEASON_FILE):
            return None, None, None
        try:
            with open(LAST_SEASON_FILE, "r") as f:
                data = json.load(f)

            expires_at = datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%S")
            if datetime.now() >= expires_at:
                os.remove(LAST_SEASON_FILE)
                return None, None, None

            season = data.get("season", "unknown")
            players = [
                {"name": info["name"], "donations": info["total"]}
                for info in data["players"].values()
            ]
            players.sort(key=lambda x: x["donations"], reverse=True)
            days_left = (expires_at - datetime.now()).days
            return players, season, days_left

        except Exception as e:
            logger.error(f"Error reading last season: {e}")
            return None, None, None


# ─── Background Sync Job ──────────────────────────────────────────────────────

async def background_sync(context):
    """Called every 60 seconds by JobQueue to silently update donation storage."""
    clan_tags = tracker.storage.get_cached_clan_tags()
    if not clan_tags:
        logger.info("Background sync skipped — no clan tags cached yet.")
        return
    tracker.sync_all_clans(clan_tags)


# ─── Bot Handlers ─────────────────────────────────────────────────────────────

tracker = None


def group_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ["group", "supergroup"]:
            await update.message.reply_text("❌ This command only works in the War Snipers group.")
            return
        await func(update, context)
    return wrapper


async def get_clan_tags_from_chat(update, context):
    chat = await context.bot.get_chat(update.effective_chat.id)
    description = chat.description or ""
    if not description:
        return []
    return tracker.parse_clan_tags(description)


@group_only
async def donation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching global donation data...")
    try:
        clan_tags = await get_clan_tags_from_chat(update, context)
        if not clan_tags:
            await update.message.reply_text("❌ No clan tags found in group description.")
            return

        tracker.storage.cache_clan_tags(clan_tags)
        players = tracker.get_all_donations(clan_tags)

        if not players:
            await update.message.reply_text("❌ Could not fetch data.")
            return

        message = tracker.format_leaderboard(players)
        if len(message) > 4096:
            for i in range(0, len(message), 4096):
                await update.message.reply_text(message[i:i + 4096])
        else:
            await update.message.reply_text(message)

    except Exception as e:
        logger.error(f"Error in /donation: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


@group_only
async def clanlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching clan data...")
    try:
        clan_tags = await get_clan_tags_from_chat(update, context)
        if not clan_tags:
            await update.message.reply_text("❌ No clan tags found.")
            return

        tracker.storage.cache_clan_tags(clan_tags)
        message = tracker.format_by_clan(clan_tags)

        if len(message) > 4096:
            parts = []
            current_part = ""
            for line in message.split("\n"):
                if len(current_part) + len(line) + 1 > 4096:
                    parts.append(current_part)
                    current_part = line + "\n"
                else:
                    current_part += line + "\n"
            if current_part:
                parts.append(current_part)
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(message)

    except Exception as e:
        logger.error(f"Error in /clanlist: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


@group_only
async def lastseason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show last season's final donation leaderboard (available for 2 weeks after season end)."""
    try:
        players, season, days_left = tracker.get_last_season_leaderboard()

        if players is None:
            await update.message.reply_text(
                "❌ No last season data available.\n"
                "Last season records are kept for 2 weeks after the season ends."
            )
            return

        title = f"📜 Last Season ({season}) Final Donations 📜\n⏳ Records expire in {days_left} day(s)"
        message = tracker.format_leaderboard(players, title=title)

        if len(message) > 4096:
            for i in range(0, len(message), 4096):
                await update.message.reply_text(message[i:i + 4096])
        else:
            await update.message.reply_text(message)

    except Exception as e:
        logger.error(f"Error in /lastseason: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


@group_only
async def checktags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        clan_tags = await get_clan_tags_from_chat(update, context)
        if not clan_tags:
            await update.message.reply_text("❌ No clan tags found.")
            return

        lines = ["📋 Detected Clan Tags:", ""]
        for tag in clan_tags:
            clan_info = tracker.api.get_clan_info(tag)
            name = clan_info.get("name", "Unknown") if clan_info else "Unknown"
            lines.append(f"▫️ {tag} ({name})")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 War Snipers Donation Tracker\n\n"
        "Commands:\n"
        "/donation - Global top donators (all clans combined)\n"
        "/clanlist - Donations grouped by clan\n"
        "/lastseason - Last season's final donation records\n"
        "/checktags - Verify detected clans\n"
        "/help - Show help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Commands:\n\n"
        "/donation - Top donators across all clans (combined rank)\n"
        "/clanlist - Donations grouped by each clan\n"
        "/lastseason - Last season's final records (kept 2 weeks)\n"
        "/checktags - See which clans I'm tracking\n\n"
        "ℹ️ Donations are synced every minute automatically.\n"
        "ℹ️ Moving between the 5 clans will not reset your donation count."
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    coc_api_token = os.getenv("COC_API_TOKEN")

    if not telegram_token or not coc_api_token:
        logger.error("Missing environment variables!")
        print("Please set TELEGRAM_TOKEN and COC_API_TOKEN")
        return

    global tracker
    tracker = DonationTracker(coc_api_token)

    application = Application.builder().token(telegram_token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("donation", donation))
    application.add_handler(CommandHandler("clanlist", clanlist))
    application.add_handler(CommandHandler("lastseason", lastseason))
    application.add_handler(CommandHandler("checktags", checktags))

    # Background sync every 60 seconds, starts 10 seconds after bot launch
    application.job_queue.run_repeating(background_sync, interval=60, first=10)

    logger.info("Bot started! Background sync active every 60 seconds.")

    import asyncio
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
