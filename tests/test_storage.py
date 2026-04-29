"""Tests for DonationStorage.

The cross-clan bonus pattern is the core value of this project — these tests
lock the move/rejoin/season behaviour and confirm v1 storage files load
unchanged after the v2 refactor.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from coc_tracker.storage import DonationStorage


# ─── v1 backward compatibility ────────────────────────────────────────────────


def test_v1_storage_loads_unchanged(tmp_storage_with_v1_data: DonationStorage):
    """A donation_storage.json from v1 must read into v2 without mutation."""
    s = tmp_storage_with_v1_data
    assert s.data["season_key"] == "20260401"
    assert s.data["clan_tags"] == ["#ABC123", "#DEF456"]
    assert s.data["clan_names"]["#ABC123"] == "War Snipers"
    assert s.data["players"]["#P1"]["bonus"] == 500
    assert s.data["players"]["#P1"]["last_donations"] == 100
    assert s.get_all_players_sorted()[0]["name"] == "Alice"  # 500+100=600 vs Bob's 250
    assert s.get_all_players_sorted()[0]["donations"] == 600


def test_old_season_key_migration(tmp_path: Path):
    """A v0 file with bare 'season' key migrates to 'season_key' on load."""
    storage_file = tmp_path / "donation_storage.json"
    storage_file.write_text(
        json.dumps({"season": "20260301", "clan_tags": [], "clan_names": {}, "players": {}})
    )
    s = DonationStorage(str(storage_file), str(tmp_path / "last.json"))
    assert "season" not in s.data
    assert "season_key" in s.data


# ─── Player update logic — the cross-clan bonus pattern ───────────────────────


def test_first_seen_player_creates_record(tmp_storage: DonationStorage):
    s = tmp_storage
    total = s.update_player("#P1", "Alice", 100, "#ABC")
    assert total == 100
    assert s.data["players"]["#P1"]["bonus"] == 0
    assert s.data["players"]["#P1"]["last_donations"] == 100
    assert s.data["players"]["#P1"]["last_clan"] == "#ABC"


def test_donation_increment_same_clan(tmp_storage: DonationStorage):
    s = tmp_storage
    s.update_player("#P1", "Alice", 100, "#ABC")
    total = s.update_player("#P1", "Alice", 250, "#ABC")
    assert total == 250
    assert s.data["players"]["#P1"]["bonus"] == 0
    assert s.data["players"]["#P1"]["last_donations"] == 250


def test_clan_move_locks_in_bonus(tmp_storage: DonationStorage):
    """When a player moves clans, previous donations roll into bonus."""
    s = tmp_storage
    s.update_player("#P1", "Alice", 100, "#ABC")
    s.update_player("#P1", "Alice", 500, "#ABC")
    # Player moves clans — CoC API resets donations to 0 on the new side
    total = s.update_player("#P1", "Alice", 0, "#DEF")
    assert total == 500  # 500 bonus + 0 current
    assert s.data["players"]["#P1"]["bonus"] == 500
    assert s.data["players"]["#P1"]["last_clan"] == "#DEF"
    assert s.data["players"]["#P1"]["last_donations"] == 0


def test_donation_drop_same_clan_treated_as_rejoin(tmp_storage: DonationStorage):
    """Same clan, donation count drops → player left and rejoined → bonus the previous total."""
    s = tmp_storage
    s.update_player("#P1", "Alice", 800, "#ABC")
    total = s.update_player("#P1", "Alice", 0, "#ABC")  # rejoined → reset to 0
    assert total == 800  # 800 bonus + 0 current
    assert s.data["players"]["#P1"]["bonus"] == 800
    assert s.data["players"]["#P1"]["last_donations"] == 0


def test_multi_hop_accumulates_correctly(tmp_storage: DonationStorage):
    """Player jumps A → B → C → A — totals should keep building."""
    s = tmp_storage
    s.update_player("#P1", "Alice", 200, "#A")
    s.update_player("#P1", "Alice", 0, "#B")  # +200 bonus
    s.update_player("#P1", "Alice", 300, "#B")
    s.update_player("#P1", "Alice", 0, "#C")  # +300 bonus = 500
    s.update_player("#P1", "Alice", 100, "#C")
    total = s.update_player("#P1", "Alice", 0, "#A")  # +100 bonus = 600
    assert total == 600
    assert s.data["players"]["#P1"]["bonus"] == 600


def test_name_change_persisted(tmp_storage: DonationStorage):
    s = tmp_storage
    s.update_player("#P1", "Alice", 100, "#ABC")
    s.update_player("#P1", "Alicia", 100, "#ABC")
    assert s.data["players"]["#P1"]["name"] == "Alicia"


# ─── Season rollover ──────────────────────────────────────────────────────────


def test_season_rollover_snapshots_and_resets(tmp_storage_with_v1_data: DonationStorage):
    """When season changes, current totals snapshot to last_season_storage.json and players reset."""
    s = tmp_storage_with_v1_data
    s.handle_season_change("20260501")  # new season

    # Snapshot file written
    snap_path = Path(s.last_season_file)
    assert snap_path.exists()
    snap = json.loads(snap_path.read_text())
    assert snap["season_key"] == "20260401"  # the OLD season is what was snapshotted
    assert snap["players"]["#P1"]["total"] == 600  # 500 bonus + 100 last_donations

    # Current data reset to new season, but clan tags/names preserved
    assert s.data["season_key"] == "20260501"
    assert s.data["players"] == {}
    assert s.data["clan_tags"] == ["#ABC123", "#DEF456"]
    assert s.data["clan_names"]["#ABC123"] == "War Snipers"


def test_season_rollover_skipped_when_unchanged(tmp_storage_with_v1_data: DonationStorage):
    """If season key matches stored, nothing happens."""
    s = tmp_storage_with_v1_data
    before_players = dict(s.data["players"])
    s.handle_season_change("20260401")  # same as stored
    assert s.data["players"] == before_players


def test_first_season_init_no_snapshot(tmp_storage: DonationStorage):
    """First-ever season key sets the field but doesn't snapshot anything (no prior season)."""
    s = tmp_storage
    s.handle_season_change("20260501")
    assert s.data["season_key"] == "20260501"
    assert not Path(s.last_season_file).exists()


def test_get_last_season_returns_sorted_with_days_left(tmp_storage_with_v1_data: DonationStorage):
    s = tmp_storage_with_v1_data
    s.handle_season_change("20260501")  # writes snapshot
    players, season_label, days_left = s.get_last_season()
    assert players is not None
    assert players[0]["name"] == "Alice"  # 600 > 250
    assert "April" in season_label and "2026" in season_label
    assert 13 <= days_left <= 14  # default retention is 14 days


def test_expired_last_season_cleared_on_read(tmp_path: Path):
    """An expired snapshot file is auto-deleted when read."""
    storage_file = tmp_path / "donation_storage.json"
    last_season_file = tmp_path / "last_season_storage.json"
    expired = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    last_season_file.write_text(
        json.dumps(
            {"season_key": "20260301", "expires_at": expired, "players": {}}
        )
    )
    s = DonationStorage(str(storage_file), str(last_season_file))
    players, season_label, days_left = s.get_last_season()
    assert players is None
    assert not last_season_file.exists()


# ─── Clan ops ─────────────────────────────────────────────────────────────────


def test_get_players_by_clan_groups_correctly(tmp_storage_with_v1_data: DonationStorage):
    s = tmp_storage_with_v1_data
    grouped = s.get_players_by_clan(["#ABC123", "#DEF456"])
    assert len(grouped) == 2
    abc_name, abc_tag, abc_players = grouped[0]
    assert abc_tag == "#ABC123"
    assert abc_players[0]["name"] == "Alice"
    assert abc_players[0]["donations"] == 600


def test_cache_clan_tags_dirties_only_on_change(tmp_storage: DonationStorage):
    s = tmp_storage
    s.cache_clan_tags(["#ABC", "#DEF"])
    assert s._dirty
    s.flush()
    s.cache_clan_tags(["#DEF", "#ABC"])  # same set, different order
    assert not s._dirty


def test_atomic_flush_uses_tmp_and_rename(tmp_storage: DonationStorage, tmp_path: Path):
    """flush() must write to .tmp and rename — partial writes can't corrupt the file."""
    s = tmp_storage
    s.cache_clan_tags(["#ABC"])
    s.flush()
    storage_path = Path(s.storage_file)
    assert storage_path.exists()
    # Confirm no leftover .tmp file
    assert not Path(f"{s.storage_file}.tmp").exists()
