"""
Donation state persistence.

Schema is preserved EXACTLY from v1 — existing donation_storage.json and
last_season_storage.json files load without modification or migration.

Schema (donation_storage.json):
{
  "season_key":  "20260401",
  "clan_tags":   ["#TAG1", ...],
  "clan_names":  {"#TAG1": "War Snipers", ...},
  "last_sync":   "2026-04-13T08:58:23",
  "players": {
    "#PLAYERTAG": {
      "name": "PlayerName", "bonus": 1000,
      "last_clan": "#TAG1", "last_donations": 250
    }
  }
}
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

from . import config
from .config import LAST_SEASON_RETENTION_DAYS

logger = logging.getLogger(__name__)


class DonationStorage:
    """Manages all donation state on disk. Schema is v1-compatible."""

    def __init__(self, storage_file: str | None = None, last_season_file: str | None = None):
        # Resolve at construction time (not import time) so env var overrides
        # set by tests / monkeypatches / late os.environ updates take effect.
        self.storage_file = storage_file or config.STORAGE_FILE
        self.last_season_file = last_season_file or config.LAST_SEASON_FILE
        self.data = self._load()
        self._dirty = False

    def _load(self) -> dict:
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file) as f:
                    data = json.load(f)
                # Migrate old "season" key to "season_key" (v0 → v1 compat, kept for safety)
                if "season" in data and "season_key" not in data:
                    data.pop("season")
                    data["season_key"] = ""
                    logger.info("Storage migrated: season_key will be set from API on first sync")
                return data
            except Exception as e:
                logger.error(f"Failed to load storage, starting fresh: {e}")
        return {"season_key": "", "clan_tags": [], "clan_names": {}, "last_sync": "", "players": {}}

    def flush(self) -> None:
        """Write to disk only when data has actually changed."""
        if not self._dirty:
            return
        try:
            tmp_path = f"{self.storage_file}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp_path, self.storage_file)
            self._dirty = False
        except Exception as e:
            logger.error(f"Failed to save storage: {e}")

    # ── Season ─────────────────────────────────────────────────────────────────

    def handle_season_change(self, new_season_key: str) -> None:
        stored_key = self.data.get("season_key", "")
        if stored_key == new_season_key:
            return
        if stored_key and new_season_key < stored_key:
            logger.warning(
                f"Ignoring backward season change: {stored_key} → {new_season_key} "
                "(API may be returning stale data)"
            )
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

    def _snapshot_to_last_season(self) -> None:
        snapshot = {
            tag: {
                "name": info.get("name", "Unknown"),
                "total": info.get("bonus", 0) + info.get("last_donations", 0),
            }
            for tag, info in self.data.get("players", {}).items()
        }
        expires_at = (datetime.now() + timedelta(days=LAST_SEASON_RETENTION_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        try:
            tmp_path = f"{self.last_season_file}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(
                    {
                        "season_key": self.data.get("season_key", ""),
                        "expires_at": expires_at,
                        "players": snapshot,
                    },
                    f,
                    indent=2,
                )
            os.replace(tmp_path, self.last_season_file)
            logger.info(f"Season snapshot saved, expires {expires_at}")
        except Exception as e:
            logger.error(f"Failed to write season snapshot: {e}")

    # ── Clan tags & names ──────────────────────────────────────────────────────

    def cache_clan_tags(self, clan_tags: list) -> None:
        if set(clan_tags) != set(self.data.get("clan_tags", [])):
            self.data["clan_tags"] = list(clan_tags)
            self._dirty = True

    def get_cached_clan_tags(self) -> list:
        return self.data.get("clan_tags", [])

    def cache_clan_name(self, clan_tag: str, name: str) -> None:
        if "clan_names" not in self.data:
            self.data["clan_names"] = {}
        if self.data["clan_names"].get(clan_tag) != name:
            self.data["clan_names"][clan_tag] = name
            self._dirty = True

    def get_clan_name(self, clan_tag: str) -> str:
        return self.data.get("clan_names", {}).get(clan_tag, clan_tag)

    # ── Last sync timestamp ────────────────────────────────────────────────────

    def update_last_sync(self) -> None:
        self.data["last_sync"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._dirty = True

    def get_last_sync(self) -> str:
        return self.data.get("last_sync", "")

    # ── Player updates ─────────────────────────────────────────────────────────

    def update_player(
        self,
        player_tag: str,
        player_name: str,
        current_donations: int,
        current_clan_tag: str,
    ) -> int:
        """
        Update one player's donation record. Returns their season total.
        Does NOT flush — caller must call flush() after a full batch.
        """
        players = self.data["players"]

        if player_tag not in players:
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
                logger.info(
                    f"[REJOIN] {player_name} back to {current_clan_tag} | bonus={p['bonus']}"
                )
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
        """Return players grouped by current clan, ordered by clan_tags. Reads from memory."""
        players = self.data.get("players", {})
        clan_names = self.data.get("clan_names", {})

        by_clan: dict = {}
        for p in players.values():
            clan = p.get("last_clan", "")
            by_clan.setdefault(clan, []).append(
                {"name": p["name"], "donations": p["bonus"] + p["last_donations"]}
            )

        results = []
        for clan_tag in clan_tags:
            if clan_tag in by_clan:
                clan_players = sorted(by_clan[clan_tag], key=lambda x: x["donations"], reverse=True)
                clan_name = clan_names.get(clan_tag, clan_tag)
                results.append((clan_name, clan_tag, clan_players))
        return results

    # ── Last season ────────────────────────────────────────────────────────────

    def get_last_season(self) -> tuple:
        """Returns (players_list, season_label, days_left) or (None, None, None)."""
        if not os.path.exists(self.last_season_file):
            return None, None, None
        try:
            with open(self.last_season_file) as f:
                data = json.load(f)
            expires_at = datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%S")
            if datetime.now() >= expires_at:
                os.remove(self.last_season_file)
                return None, None, None
            players = sorted(
                [{"name": v["name"], "donations": v["total"]} for v in data["players"].values()],
                key=lambda x: x["donations"],
                reverse=True,
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

    def cleanup_last_season_if_expired(self) -> None:
        self.get_last_season()
