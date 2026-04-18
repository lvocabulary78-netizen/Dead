#!/usr/bin/env python3
"""
bot.py — Entry point for the Synonym Game Bot.

Usage:
    export BOT_TOKEN="your_token"
    python bot.py
"""

import logging
import telebot

from config import BOT_TOKEN
from db import init_db
from word_cache import load_words
import handlers

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    # 1. Set up the database
    init_db()

    # 2. Load word cache into memory
    load_words()

    # 3. Create and configure the bot
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
    bot.remove_webhook()   # clear any leftover webhook

    # 4. Register all handlers
    handlers.register(bot)
    logger.info("All handlers registered. Starting polling…")

    # 5. Run
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if __name__ == "__main__":
    main()
