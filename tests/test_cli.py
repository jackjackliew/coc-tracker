"""Tests for the CLI tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def populated_cli_env(tmp_path: Path, monkeypatch):
    """Point the CLI's DonationStorage at a tmp file pre-populated with data."""
    storage_file = tmp_path / "donation_storage.json"
    last_season_file = tmp_path / "last_season_storage.json"
    initial = {
        "season_key": "20260401",
        "clan_tags": ["#ABC"],
        "clan_names": {"#ABC": "War Snipers"},
        "last_sync": "2026-04-13T08:58:23",
        "players": {
            "#P1": {"name": "Alice", "bonus": 500, "last_clan": "#ABC", "last_donations": 100},
            "#P2": {"name": "Bob", "bonus": 0, "last_clan": "#ABC", "last_donations": 250},
        },
    }
    storage_file.write_text(json.dumps(initial))
    monkeypatch.setattr("coc_tracker.config.STORAGE_FILE", str(storage_file))
    monkeypatch.setattr("coc_tracker.config.LAST_SEASON_FILE", str(last_season_file))
    return tmp_path


def test_stats_prints_top_donators(populated_cli_env, capsys):
    from coc_tracker import cli

    rc = cli.main(["stats", "--top", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Alice" in out
    assert "600" in out  # 500 bonus + 100 last_donations
    assert "Bob" in out
    # Alice ranks above Bob
    assert out.index("Alice") < out.index("Bob")


def test_export_csv(populated_cli_env, capsys):
    from coc_tracker import cli

    rc = cli.main(["export", "--format", "csv"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.strip().splitlines()
    assert lines[0] == "rank,name,donations"
    assert "1,Alice,600" in out
    assert "2,Bob,250" in out


def test_export_json(populated_cli_env, capsys):
    from coc_tracker import cli

    rc = cli.main(["export", "--format", "json"])
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["season_key"] == "20260401"
    assert parsed["players"][0]["name"] == "Alice"


def test_verify_ok(populated_cli_env, capsys):
    from coc_tracker import cli

    rc = cli.main(["verify"])
    assert rc == 0
    assert "Storage OK" in capsys.readouterr().out


def test_verify_detects_missing_field(tmp_path, monkeypatch, capsys):
    from coc_tracker import cli

    storage_file = tmp_path / "donation_storage.json"
    storage_file.write_text(
        json.dumps(
            {
                "season_key": "20260401",
                "clan_tags": [],
                "clan_names": {},
                "last_sync": "",
                "players": {
                    "#P1": {"name": "Alice", "bonus": 100}
                },  # missing last_clan, last_donations
            }
        )
    )
    monkeypatch.setattr("coc_tracker.config.STORAGE_FILE", str(storage_file))
    monkeypatch.setattr("coc_tracker.config.LAST_SEASON_FILE", str(tmp_path / "ls.json"))
    rc = cli.main(["verify"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "missing" in err.lower()


def test_backups_empty(tmp_path, monkeypatch, capsys):
    from coc_tracker import backup as backup_module
    from coc_tracker import cli

    monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "no-backups")
    rc = cli.main(["backups"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "No backups" in err
