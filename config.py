"""
config.py — Central configuration for the Synonym Game Bot.
Edit BOT_TOKEN and SUPER_ADMIN_IDS before running.
"""

import os

# ── Bot credentials ────────────────────────────────────────────────────────
BOT_TOKEN = "8732231617:AAHY6afV8Qmd_rfBgFDIDAMXUe7bbeATedY"

# Telegram user IDs of global super-admins (can use /ban, /unban anywhere)
SUPER_ADMIN_IDS: list[int] = [7161553913,6526832001]

# ── File paths ─────────────────────────────────────────────────────────────
DB_PATH    = "synonym_game.db"
WORDS_FILE = "words.json"

# ── Default game settings ──────────────────────────────────────────────────
DEFAULT_NUM_QUESTIONS  = 10
DEFAULT_TIME_PER_ROUND = 90   # seconds

# ── Scoring ────────────────────────────────────────────────────────────────
CORRECT_POINTS = 5
HINT_COST      = 2   # deducted from session score only
SKIP_COST      = 1   # deducted from session score only

# ── Supported levels ────────────────────────────────────────────────────────
LEVELS = ["A1", "A2", "B1"]
