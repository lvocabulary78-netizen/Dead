"""
db.py — SQLite database layer.
All persistent state lives here: groups, users, game settings.
The word list itself is kept in memory (word_cache.py).
"""

import sqlite3
import logging
from config import DB_PATH

logger = logging.getLogger(__name__)


# ── Connection helper ─────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")   # safer for concurrent access
    return c


# ── Schema ────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables if they don't exist."""
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id        INTEGER PRIMARY KEY,
                level           TEXT    NOT NULL DEFAULT 'A1',
                activated       INTEGER NOT NULL DEFAULT 0,
                activated_by    INTEGER
            );

            CREATE TABLE IF NOT EXISTS game_settings (
                group_id         INTEGER PRIMARY KEY,
                num_questions    INTEGER NOT NULL DEFAULT 10,
                time_per_round   INTEGER NOT NULL DEFAULT 90,
                hints_enabled    INTEGER NOT NULL DEFAULT 1,
                skip_enabled     INTEGER NOT NULL DEFAULT 1,
                require_approval INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (group_id) REFERENCES groups(group_id)
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                total_points INTEGER NOT NULL DEFAULT 0,
                wins         INTEGER NOT NULL DEFAULT 0,
                losses       INTEGER NOT NULL DEFAULT 0,
                is_banned    INTEGER NOT NULL DEFAULT 0
            );
        """)
    logger.info("Database initialised at %s", DB_PATH)


# ── Group operations ──────────────────────────────────────────────────────

def activate_group(group_id: int, level: str, admin_id: int) -> None:
    with _conn() as con:
        con.execute("""
            INSERT INTO groups (group_id, level, activated, activated_by)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                level        = excluded.level,
                activated    = 1,
                activated_by = excluded.activated_by
        """, (group_id, level, admin_id))
        con.execute(
            "INSERT OR IGNORE INTO game_settings (group_id) VALUES (?)",
            (group_id,)
        )


def get_group(group_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM groups WHERE group_id = ?", (group_id,)
        ).fetchone()
    return dict(row) if row else None


def update_group_level(group_id: int, level: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE groups SET level = ? WHERE group_id = ?", (level, group_id)
        )


# ── Game-settings operations ───────────────────────────────────────────────

def get_game_settings(group_id: int) -> dict:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM game_settings WHERE group_id = ?", (group_id,)
        ).fetchone()
    if row:
        return dict(row)
    # Return safe defaults if the row doesn't exist yet
    return {
        "group_id": group_id,
        "num_questions": 10,
        "time_per_round": 90,
        "hints_enabled": 1,
        "skip_enabled": 1,
        "require_approval": 0,
    }


def update_game_settings(group_id: int, **kwargs) -> None:
    """Update one or more settings columns dynamically."""
    if not kwargs:
        return
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [group_id]
    with _conn() as con:
        con.execute(
            f"UPDATE game_settings SET {set_clause} WHERE group_id = ?",
            values
        )


# ── User operations ───────────────────────────────────────────────────────

def ensure_user(user_id: int, username: str | None) -> None:
    """Insert or update a user record (upsert by user_id)."""
    with _conn() as con:
        con.execute("""
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
        """, (user_id, username))


def get_user(user_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def is_banned(user_id: int) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT is_banned FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return bool(row and row["is_banned"])


def ban_user(user_id: int) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO users (user_id, is_banned) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET is_banned = 1",
            (user_id,)
        )


def unban_user(user_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,)
        )


def add_points(user_id: int, points: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE users SET total_points = total_points + ? WHERE user_id = ?",
            (points, user_id)
        )


def record_game_result(user_id: int, points: int, won: bool) -> None:
    """Persist end-of-game stats for a player."""
    ensure_user(user_id, None)
    with _conn() as con:
        if won:
            con.execute("""
                UPDATE users
                SET total_points = total_points + ?,
                    wins         = wins + 1
                WHERE user_id = ?
            """, (points, user_id))
        else:
            con.execute("""
                UPDATE users
                SET total_points = total_points + ?,
                    losses       = losses + 1
                WHERE user_id = ?
            """, (points, user_id))


def get_global_leaderboard(limit: int = 10) -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT user_id, username, total_points, wins, losses
            FROM   users
            WHERE  is_banned = 0
            ORDER  BY total_points DESC
            LIMIT  ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]
