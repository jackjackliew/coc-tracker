"""Tests for the `coc-tracker disk` subcommand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def disk_env(tmp_path: Path, monkeypatch):
    """Point storage + backup dirs + HOME at tmp_path so the disk command sees a known footprint."""
    storage_file = tmp_path / "donation_storage.json"
    last_season_file = tmp_path / "last_season_storage.json"
    storage_file.write_text(json.dumps({"season_key": "20260401", "players": {}}))
    last_season_file.write_text(json.dumps({"season_key": "20260301", "players": {}}))

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "2026-04-29T00-00-00").mkdir()
    (backup_dir / "2026-04-29T00-00-00" / "donation_storage.json").write_text("{}")

    # Pre-deploy backup directories in HOME
    (tmp_path / "coc-tracker-pre-deploy-backup-20260429-180000").mkdir()
    (
        tmp_path / "coc-tracker-pre-deploy-backup-20260429-180000" / "donation_storage.json"
    ).write_text("{}")

    monkeypatch.setattr("coc_tracker.config.STORAGE_FILE", str(storage_file))
    monkeypatch.setattr("coc_tracker.config.LAST_SEASON_FILE", str(last_season_file))
    monkeypatch.setattr("coc_tracker.config.REPO_ROOT", tmp_path)

    from coc_tracker import backup as backup_module

    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    return tmp_path


def test_disk_runs_and_lists_known_buckets(disk_env, capsys):
    from coc_tracker import cli

    rc = cli.main(["disk"])
    out = capsys.readouterr().out
    assert rc in (0, 1)  # 1 only if we trip a warning, not relevant here
    assert "donation_storage.json" in out
    assert "auto-backups" in out
    assert "pre-deploy backups" in out
    assert "TOTAL bot-owned" in out


def test_disk_warns_on_excess_pre_deploy_backups(tmp_path, monkeypatch, capsys):
    from coc_tracker import backup as backup_module
    from coc_tracker import cli

    storage_file = tmp_path / "donation_storage.json"
    storage_file.write_text("{}")
    monkeypatch.setattr("coc_tracker.config.STORAGE_FILE", str(storage_file))
    monkeypatch.setattr(
        "coc_tracker.config.LAST_SEASON_FILE", str(tmp_path / "last_season_storage.json")
    )
    monkeypatch.setattr("coc_tracker.config.REPO_ROOT", tmp_path)
    monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setenv("HOME", str(tmp_path))

    # Create 12 pre-deploy backup dirs to trigger the warning
    for i in range(12):
        (tmp_path / f"coc-tracker-pre-deploy-backup-2026-04-{i:02d}").mkdir()

    rc = cli.main(["disk"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "pre-deploy backups present" in err
