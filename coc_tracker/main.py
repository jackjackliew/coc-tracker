"""Application entrypoint."""

from __future__ import annotations

import logging
import os

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

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


def main() -> None:
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    coc_api_token = os.getenv("COC_API_TOKEN")
    if not telegram_token or not coc_api_token:
        logger.error("Missing TELEGRAM_TOKEN or COC_API_TOKEN environment variables.")
        return

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

    app.job_queue.run_repeating(background_sync, interval=POLL_INTERVAL, first=5)
    logger.info(f"Bot started. Syncing every {POLL_INTERVAL}s.")

    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        # Best-effort cleanup of the async HTTP client
        import asyncio

        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(tracker.aclose())
            loop.close()
        except Exception as e:
            logger.warning(f"Error closing HTTP client: {e}")


if __name__ == "__main__":
    main()
