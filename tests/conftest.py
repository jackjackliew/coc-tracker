"""Pytest fixtures."""

import json
from pathlib import Path

import pytest

from coc_tracker.storage import DonationStorage


@pytest.fixture
def tmp_storage(tmp_path: Path) -> DonationStorage:
    storage_file = tmp_path / "donation_storage.json"
    last_season_file = tmp_path / "last_season_storage.json"
    return DonationStorage(str(storage_file), str(last_season_file))


@pytest.fixture
def tmp_storage_with_v1_data(tmp_path: Path) -> DonationStorage:
    """Storage pre-populated with the exact schema produced by v1 — must round-trip cleanly."""
    storage_file = tmp_path / "donation_storage.json"
    last_season_file = tmp_path / "last_season_storage.json"
    initial = {
        "season_key": "20260401",
        "clan_tags": ["#ABC123", "#DEF456"],
        "clan_names": {"#ABC123": "War Snipers", "#DEF456": "War Snipers 2"},
        "last_sync": "2026-04-13T08:58:23",
        "players": {
            "#P1": {"name": "Alice", "bonus": 500, "last_clan": "#ABC123", "last_donations": 100},
            "#P2": {"name": "Bob", "bonus": 0, "last_clan": "#DEF456", "last_donations": 250},
        },
    }
    storage_file.write_text(json.dumps(initial, indent=2))
    return DonationStorage(str(storage_file), str(last_season_file))
