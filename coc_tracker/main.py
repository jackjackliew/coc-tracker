"""Application entrypoint."""

from __future__ import annotations

import asyncio
import logging
import os

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from .backup import BACKUP_INTERVAL_HOURS, backup_job
from .config import POLL_INTERVAL
from .handlers import (
    background_sync,
    button_handler,
    checktags,
    clanlist,
    donation,
    help_cmd,
    lastseason,
    menu,
    set_tracker,
    start,
)
from .tracker import DonationTracker

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger(__name__)


def _quiet_third_party_logs() -> None:
    """Reduce log volume from libraries that log every operation at INFO level.

    On a free-tier VM with capped journald (50–100 MB), httpx+apscheduler INFO
    logs at 10s sync cadence balloon the journal by ~17 MB/day. Promoting them
    to WARNING drops journal growth to ~2 MB/day with no operational loss
    (errors still surface; sync success is observable via `coc-tracker stats`).

    Override with `LOG_VERBOSE_LIBS=1` if you need request-by-request tracing.
    """
    if os.getenv("LOG_VERBOSE_LIBS", "0") == "1":
        return
    for noisy in ("httpx", "httpcore", "apscheduler", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    coc_api_token = os.getenv("COC_API_TOKEN")
    if not telegram_token or not coc_api_token:
        logger.error("Missing TELEGRAM_TOKEN or COC_API_TOKEN environment variables.")
        return

    _quiet_third_party_logs()

    tracker = DonationTracker(coc_api_token)
    set_tracker(tracker)

    app = Application.builder().token(telegram_token).build()

    for cmd, fn in [
        ("start", start),
        ("help", help_cmd),
        ("menu", menu),
        ("donation", donation),
        ("clanlist", clanlist),
        ("lastseason", lastseason),
        ("checktags", checktags),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Background donation sync
    app.job_queue.run_repeating(background_sync, interval=POLL_INTERVAL, first=5)

    # Defence-in-depth: periodic JSON snapshots of the live storage
    backup_seconds = max(BACKUP_INTERVAL_HOURS * 3600, 600)
    app.job_queue.run_repeating(backup_job, interval=backup_seconds, first=backup_seconds)

    logger.info(
        f"Bot started. Syncing every {POLL_INTERVAL}s. Backups every {BACKUP_INTERVAL_HOURS}h."
    )

    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        # Best-effort cleanup of the async HTTP client
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(tracker.aclose())
            loop.close()
        except Exception as e:
            logger.warning(f"Error closing HTTP client: {e}")


if __name__ == "__main__":
    main()
