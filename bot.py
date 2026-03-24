import os
import json
import logging
import requests
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

COC_API_BASE = "https://api.clashofclans.com/v1"
STORAGE_FILE = os.path.join(os.path.dirname(__file__), "donation_storage.json")


# ─── Season & Storage ────────────────────────────────────────────────────────

def get_current_season():
    now = datetime.now()
    return f"{now.year}-{now.month:02d}"


class DonationStorage:
    """
    Persists each player's 'bonus' donations earned in previous clans
    so that moving between clans doesn't wipe their season total.
    
    Storage schema:
    {
      "season": "2026-03",
      "players": {
        "#PLAYERTAG": {
          "name": "PlayerName",
          "bonus": 1234,          # donations carried from previous clan(s)
          "last_clan": "#CLANTAG",
          "last_donations": 567   # last known API donation count in last_clan
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
        return {"season": get_current_season(), "players": {}}

    def _save(self):
        try:
            with open(STORAGE_FILE, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save storage: {e}")

    def _check_season_reset(self):
        """Clear storage when a new CoC season starts (new month)."""
        current = get_current_season()
        if self.data.get("season") != current:
            logger.info(f"New season detected ({current}), resetting storage.")
            self.data = {"season": current, "players": {}}
            self._save()

    def update_and_get_total(self, player_tag, player_name, current_donations, current_clan_tag):
        """
        Update stored record for this player and return their season total.
        If the player has moved to a new clan, their previous clan's donations
        are added to their bonus before resetting the tracking.
        """
        self._check_season_reset()
        players = self.data["players"]

        if player_tag not in players:
            # First time seeing this player this season
            players[player_tag] = {
                "name": player_name,
                "bonus": 0,
                "last_clan": current_clan_tag,
                "last_donations": current_donations,
            }
        else:
            stored = players[player_tag]
            stored["name"] = player_name  # keep name current

            if stored["last_clan"] != current_clan_tag:
                # Player switched clans — carry over their previous clan donations
                stored["bonus"] += stored["last_donations"]
                stored["last_clan"] = current_clan_tag
                stored["last_donations"] = current_donations
                logger.info(
                    f"{player_name} ({player_tag}) moved clan. "
                    f"Bonus now {stored['bonus']}, current donations {current_donations}"
                )
            else:
                # Same clan — just update the latest donation count
                stored["last_donations"] = current_donations

        self._save()
        total = players[player_tag]["bonus"] + current_donations
        return total


# ─── CoC API ─────────────────────────────────────────────────────────────────

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


# ─── Tracker ─────────────────────────────────────────────────────────────────

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

    def get_all_donations(self, clan_tags):
        """
        Return combined leaderboard across all clans.
        Each player's total = current API donations + bonus from previous clans.
        Players who appear in multiple clans are deduplicated by player tag.
        """
        seen_tags = set()
        all_players = []

        for clan_tag in clan_tags:
            data = self.api.get_clan_members(clan_tag)
            if not data or "items" not in data:
                continue
            for member in data["items"]:
                player_tag = member.get("tag", "")
                player_name = member.get("name", "Unknown")
                current_donations = member.get("donations", 0)

                if player_tag in seen_tags:
                    continue  # skip duplicates (shouldn't happen but safety net)
                seen_tags.add(player_tag)

                total = self.storage.update_and_get_total(
                    player_tag, player_name, current_donations, clan_tag
                )
                all_players.append({
                    "name": player_name,
                    "donations": total,
                    "clan_tag": clan_tag,
                })

        all_players.sort(key=lambda x: x["donations"], reverse=True)
        return all_players

    def get_clan_donations(self, clan_tag):
        """
        Return per-clan leaderboard with season totals (including carry-over).
        """
        clan_info = self.api.get_clan_info(clan_tag)
        members_data = self.api.get_clan_members(clan_tag)

        if not members_data or "items" not in members_data:
            return None, None

        clan_name = clan_info.get("name", "Unknown Clan") if clan_info else "Unknown Clan"
        players = []

        for member in members_data["items"]:
            player_tag = member.get("tag", "")
            player_name = member.get("name", "Unknown")
            current_donations = member.get("donations", 0)

            total = self.storage.update_and_get_total(
                player_tag, player_name, current_donations, clan_tag
            )
            players.append({
                "name": player_name,
                "donations": total,
                "clan_tag": clan_tag,
                "clan_name": clan_name,
            })

        players.sort(key=lambda x: x["donations"], reverse=True)
        return players, clan_name

    def format_leaderboard(self, players):
        if not players:
            return "❌ No donation data found."
        timestamp = datetime.now().strftime("%-m/%-d/%Y, %-I:%M:%S %p")
        lines = ["🏆 Top Donators This Season 🏆", ""]
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
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
            players, clan_name = self.get_clan_donations(tag)
            if players:
                lines.append(f"{tag}")
                for idx, player in enumerate(players, 1):
                    medal = medals.get(idx, "▫️")
                    lines.append(f"{medal} {player['name']} - {player['donations']:,}")
                lines.append("")
        lines.append(f"Last updated: {timestamp}")
        return "\n".join(lines)


# ─── Bot handlers ─────────────────────────────────────────────────────────────

tracker = None


async def donation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            "❌ This command only works in the War Snipers group.\n"
            "Please use it there so I can read the clan tags from the group description."
        )
        return

    await update.message.reply_text("⏳ Fetching global donation data...")

    try:
        chat = await context.bot.get_chat(update.effective_chat.id)
        description = chat.description or ""
        if not description:
            await update.message.reply_text("❌ No group description found.")
            return

        clan_tags = tracker.parse_clan_tags(description)
        if not clan_tags:
            await update.message.reply_text("❌ No clan tags found in description.")
            return

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


async def clanlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("❌ This command only works in the War Snipers group.")
        return

    await update.message.reply_text("⏳ Fetching clan data...")

    try:
        chat = await context.bot.get_chat(update.effective_chat.id)
        description = chat.description or ""
        if not description:
            await update.message.reply_text("❌ No group description found.")
            return

        clan_tags = tracker.parse_clan_tags(description)
        if not clan_tags:
            await update.message.reply_text("❌ No clan tags found.")
            return

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 War Snipers Donation Tracker\n\n"
        "Commands:\n"
        "/donation - Global top donators (all clans combined)\n"
        "/clanlist - Donations grouped by clan\n"
        "/checktags - Verify detected clans\n"
        "/help - Show help"
    )


async def checktags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("❌ This command only works in the group.")
        return
    try:
        chat = await context.bot.get_chat(update.effective_chat.id)
        description = chat.description or ""
        if not description:
            await update.message.reply_text("❌ No description found.")
            return

        clan_tags = tracker.parse_clan_tags(description)
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


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Commands:\n\n"
        "/donation - Top donators across all clans (combined rank)\n"
        "/clanlist - Donations grouped by each clan\n"
        "/checktags - See which clans I'm tracking\n\n"
        "Make sure clan tags are in the group description!\n\n"
        "ℹ️ Donation totals are tracked across clan moves within the same season."
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
    application.add_handler(CommandHandler("checktags", checktags))

    logger.info("Bot started!")

    import asyncio
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
