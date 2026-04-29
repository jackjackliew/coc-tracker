"""Periodic JSON backup — defence-in-depth for the donation record.

Every ``BACKUP_INTERVAL_HOURS`` (default 6) the storage files are copied into
a rotating backup directory. The most recent ``BACKUP_RETENTION`` snapshots
(default 30 ≈ 7.5 days at 6h cadence) are kept; older ones are pruned.

This is belt-and-suspenders. The primary source of truth remains the live
JSON files in the working directory. Backups exist purely as a recovery
option if those files are ever damaged.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.getenv("COC_BACKUP_DIR", str(config.REPO_ROOT / "backups")))
BACKUP_INTERVAL_HOURS = int(os.getenv("COC_BACKUP_INTERVAL_HOURS", "6"))
BACKUP_RETENTION = int(os.getenv("COC_BACKUP_RETENTION", "30"))

# Internal flag: skip the very first scheduled run when storage is still empty.
_first_call = True


def make_backup() -> Path | None:
    """Snapshot current storage files into ``BACKUP_DIR/<timestamp>/``.

    Returns the snapshot directory, or ``None`` if no source files exist yet.
    Uses late-binding lookup of ``config.STORAGE_FILE`` / ``config.LAST_SEASON_FILE``
    so tests / runtime overrides take effect without re-importing.
    """
    storage_file = config.STORAGE_FILE
    last_season_file = config.LAST_SEASON_FILE

    if not (Path(storage_file).exists() or Path(last_season_file).exists()):
        logger.debug("Backup skipped — no storage files yet")
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    snap = BACKUP_DIR / stamp
    snap.mkdir(parents=True, exist_ok=True)

    copied = 0
    for src in (storage_file, last_season_file):
        src_path = Path(src)
        if src_path.exists():
            try:
                shutil.copy2(src_path, snap / src_path.name)
                copied += 1
            except OSError as e:
                logger.error(f"Backup copy failed for {src}: {e}")

    if copied == 0:
        snap.rmdir()
        return None

    logger.info(f"Backup written: {snap} ({copied} file(s))")
    _prune(BACKUP_DIR, BACKUP_RETENTION)
    return snap


def _prune(backup_dir: Path, keep: int) -> None:
    """Keep only the most recent ``keep`` snapshot directories."""
    if keep <= 0:
        return
    snaps = sorted([p for p in backup_dir.iterdir() if p.is_dir()], reverse=True)
    for stale in snaps[keep:]:
        try:
            shutil.rmtree(stale)
            logger.debug(f"Pruned old backup: {stale}")
        except OSError as e:
            logger.warning(f"Failed to prune {stale}: {e}")


# ─── PTB job-queue hook ───────────────────────────────────────────────────────


async def backup_job(context) -> None:
    """Telegram JobQueue hook: skip first invocation, then snapshot on schedule."""
    global _first_call
    if _first_call:
        _first_call = False
        return
    try:
        make_backup()
    except Exception as e:
        logger.error(f"Backup job error: {e}")
