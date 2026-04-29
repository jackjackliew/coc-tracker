"""Donation tracker orchestrator — async."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from .api import ClashAPI
from .config import CLAN_NAME_REFRESH, SEASON_CHECK_INTERVAL
from .storage import DonationStorage

logger = logging.getLogger(__name__)


class DonationTracker:
    def __init__(self, api_token: str):
        self.api = ClashAPI(api_token)
        self.storage = DonationStorage()
        self._last_season_check = datetime.min
        self._last_clan_name_refresh = datetime.min

    async def aclose(self) -> None:
        await self.api.aclose()

    async def _refresh_season_if_due(self) -> None:
        now = datetime.now()
        if (now - self._last_season_check).total_seconds() < SEASON_CHECK_INTERVAL:
            return
        self._last_season_check = now
        season_key = await self.api.get_season_key()
        if season_key:
            self.storage.handle_season_change(season_key)
        else:
            fallback = self.storage.data.get("season_key") or datetime.now().strftime("%Y%m01")
            logger.debug(f"Goldpass API unavailable (non-critical), using key: {fallback}")
            self.storage.handle_season_change(fallback)

    async def _refresh_clan_names_if_due(self, clan_tags: list) -> None:
        """Refresh clan names from API once per hour — they rarely change."""
        now = datetime.now()
        if (now - self._last_clan_name_refresh).total_seconds() < CLAN_NAME_REFRESH:
            return
        self._last_clan_name_refresh = now
        for clan_tag in clan_tags:
            info = await self.api.get_clan_info(clan_tag)
            if info and "name" in info:
                self.storage.cache_clan_name(clan_tag, info["name"])
        logger.debug(f"Clan names refreshed for {len(clan_tags)} clans")

    @staticmethod
    def parse_clan_tags(description: str) -> list:
        tags = re.findall(r"#[A-Z0-9]+", description.upper())
        seen, unique = set(), []
        for tag in tags:
            if tag not in seen:
                seen.add(tag)
                unique.append(tag)
        return unique

    async def sync_all_clans(self, clan_tags: list) -> None:
        """Background sync: poll every clan, update all member records. Single disk write per cycle."""
        if not clan_tags:
            return

        await self._refresh_season_if_due()
        await self._refresh_clan_names_if_due(clan_tags)

        seen_players = set()
        for clan_tag in clan_tags:
            data = await self.api.get_clan_members(clan_tag)
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

    def format_leaderboard(
        self, players: list, title: str = "🏆 Top Donators This Season 🏆"
    ) -> str:
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

    async def get_all_donations_fresh(self, clan_tags: list) -> list:
        """Force fresh API call — used only when cache is empty (first run)."""
        await self._refresh_season_if_due()
        seen_players = set()
        players = []
        for clan_tag in clan_tags:
            data = await self.api.get_clan_members(clan_tag)
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
