"""Telegram inline keyboard builders."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def build_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏆 Donations", callback_data="donation"),
                InlineKeyboardButton("📊 Clan List", callback_data="clanlist"),
            ],
            [
                InlineKeyboardButton("📜 Last Season", callback_data="lastseason"),
                InlineKeyboardButton("🔍 Check Tags", callback_data="checktags"),
            ],
        ]
    )
