"""Configuration constants and environment-loaded settings."""

import os
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
STORAGE_FILE = os.getenv("COC_STORAGE_FILE", str(REPO_ROOT / "donation_storage.json"))
LAST_SEASON_FILE = os.getenv("COC_LAST_SEASON_FILE", str(REPO_ROOT / "last_season_storage.json"))

# ─── Intervals (seconds) ──────────────────────────────────────────────────────

POLL_INTERVAL = int(os.getenv("COC_POLL_INTERVAL", "10"))
SEASON_CHECK_INTERVAL = int(os.getenv("COC_SEASON_CHECK_INTERVAL", "600"))
CLAN_NAME_REFRESH = int(os.getenv("COC_CLAN_NAME_REFRESH", "3600"))

# ─── CoC API ──────────────────────────────────────────────────────────────────

COC_API_BASE = "https://api.clashofclans.com/v1"
HTTP_TIMEOUT = float(os.getenv("COC_HTTP_TIMEOUT", "10"))

# ─── Telegram ─────────────────────────────────────────────────────────────────

TELEGRAM_MESSAGE_LIMIT = 4096
LAST_SEASON_RETENTION_DAYS = int(os.getenv("COC_LAST_SEASON_RETENTION_DAYS", "14"))
