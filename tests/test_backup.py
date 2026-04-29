"""Tests for the backup module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from coc_tracker import backup as backup_module


def test_make_backup_copies_existing_files(tmp_path: Path):
    storage = tmp_path / "donation_storage.json"
    last_season = tmp_path / "last_season_storage.json"
    storage.write_text(json.dumps({"season_key": "20260401"}))
    last_season.write_text(json.dumps({"season_key": "20260301"}))
    backup_dir = tmp_path / "backups"

    with (
        patch("coc_tracker.config.STORAGE_FILE", str(storage)),
        patch("coc_tracker.config.LAST_SEASON_FILE", str(last_season)),
        patch.object(backup_module, "BACKUP_DIR", backup_dir),
    ):
        snap = backup_module.make_backup()

    assert snap is not None
    assert (snap / "donation_storage.json").exists()
    assert (snap / "last_season_storage.json").exists()
    assert json.loads((snap / "donation_storage.json").read_text())["season_key"] == "20260401"


def test_make_backup_returns_none_when_no_storage(tmp_path: Path):
    """If neither storage file exists, no backup is created."""
    with (
        patch("coc_tracker.config.STORAGE_FILE", str(tmp_path / "missing.json")),
        patch("coc_tracker.config.LAST_SEASON_FILE", str(tmp_path / "also-missing.json")),
        patch.object(backup_module, "BACKUP_DIR", tmp_path / "backups"),
    ):
        snap = backup_module.make_backup()
    assert snap is None


def test_prune_keeps_most_recent_n(tmp_path: Path):
    """Older snapshots beyond the retention count are removed."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for stamp in [
        "2026-04-25T00-00-00",
        "2026-04-26T00-00-00",
        "2026-04-27T00-00-00",
        "2026-04-28T00-00-00",
        "2026-04-29T00-00-00",
    ]:
        (backup_dir / stamp).mkdir()

    backup_module._prune(backup_dir, keep=3)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == [
        "2026-04-27T00-00-00",
        "2026-04-28T00-00-00",
        "2026-04-29T00-00-00",
    ]


def test_prune_no_op_when_under_threshold(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "2026-04-29T00-00-00").mkdir()
    backup_module._prune(backup_dir, keep=10)
    assert (backup_dir / "2026-04-29T00-00-00").exists()
