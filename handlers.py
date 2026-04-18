"""
handlers.py — All Telegram message and callback-query handlers.

Design notes:
  • active_games maps group_id → GameSession (running game)
    or group_id → dict (pending setup state).
  • _get_session() only returns a real GameSession, never a setup dict.
  • All timer callbacks use claim_round() to avoid double-firing.
  • Updated to support "examples" list instead of single "example" string.
"""

import threading
import logging
from typing import Union

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    Message, CallbackQuery,
)

import db
import word_cache
from game_logic import GameSession
from config import (
    CORRECT_POINTS, HINT_COST, SKIP_COST, SUPER_ADMIN_IDS,
    DEFAULT_NUM_QUESTIONS, DEFAULT_TIME_PER_ROUND,
)

logger = logging.getLogger(__name__)

# ── Module-level state ─────────────────────────────────────────────────────
# Stores either a GameSession (active) or a plain dict (setup in progress)
active_games: dict[int, Union[GameSession, dict]] = {}

_bot: telebot.TeleBot | None = None   # set in register()


# ══════════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_session(group_id: int) -> GameSession | None:
    """Return the GameSession for a group, or None if not active."""
    obj = active_games.get(group_id)
    return obj if isinstance(obj, GameSession) else None


def _is_group_admin(chat_id: int, user_id: int) -> bool:
    try:
        admins = _bot.get_chat_administrators(chat_id)
        return any(a.user.id == user_id for a in admins)
    except Exception:
        return False


def _is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS


# ── Keyboard builders ──────────────────────────────────────────────────────

def _level_kbd() -> InlineKeyboardMarkup:
    kbd = InlineKeyboardMarkup()
    kbd.row(
        InlineKeyboardButton("🟢 A1 — Beginner",      callback_data="act_level:A1"),
        InlineKeyboardButton("🟡 A2 — Elementary",    callback_data="act_level:A2"),
    )
    kbd.add(InlineKeyboardButton("🟠 B1 — Intermediate", callback_data="act_level:B1"))
    return kbd


def _category_kbd(level: str) -> InlineKeyboardMarkup:
    cats = word_cache.get_categories(level)
    kbd  = InlineKeyboardMarkup(row_width=2)
    btns = [InlineKeyboardButton(c, callback_data=f"cat:{c}") for c in cats]
    kbd.add(*btns)
    return kbd


# ── Messaging helpers ───────────────────────────────────────────────────────

def _send_word(group_id: int, session: GameSession) -> None:
    """Announce the current word and start the round timer."""
    w = session.current_word
    msg = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 <b>Round {session.round_number} / {session.total_rounds}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔤 Find a synonym for:\n\n"
        f"<b>  ≫  {w['word'].upper()}  ≪</b>\n\n"
        f"⏱ <b>{session.time_per_round}s</b> on the clock!\n"
    )
    if session.hints_enabled or session.skip_enabled:
        tips = []
        if session.hints_enabled:
            tips.append(f"/hint  (−{HINT_COST} pts)")
        if session.skip_enabled:
            tips.append(f"/skip  (−{SKIP_COST} pt)")
        msg += "\n💡 " + "  |  ".join(tips)

    _bot.send_message(group_id, msg)

    # Start the countdown
    def on_timeout():
        _handle_timeout(group_id)
    session.start_timer(on_timeout)


def _send_round_result(
    group_id: int,
    session:  GameSession,
    winner:   str | None = None,
) -> None:
    """Reveal the answer details after a round ends."""
    w        = session.current_word
    synonyms = " / ".join(w["synonyms"])

    # Get all examples, fallback to old format if needed
    examples = w.get("examples", [])
    if not examples and "example" in w:
        examples = [w["example"]]
    
    example_text = ""
    if examples:
        if len(examples) == 1:
            example_text = f"<i>{examples[0]}</i>"
        else:
            # Show all examples with bullet points
            example_text = "\n".join(f"• <i>{ex}</i>" for ex in examples)

    header = (
        f"✅ <b>{winner}</b> got it! +{CORRECT_POINTS} pts"
        if winner else
        "⏰ Time's up — no one answered."
    )
    
    msg = (
        f"{header}\n\n"
        f"📚 <b>Word:</b>      {w['word']}\n"
        f"✔️ <b>Synonyms:</b>  {synonyms}\n"
        f"🇸🇦 <b>Arabic:</b>    {w['arabic']}\n"
        f"💬 <b>Example(s):</b>\n{example_text}"
    )
    _bot.send_message(group_id, msg)


def _send_final_leaderboard(group_id: int, session: GameSession) -> None:
    """Send the end-of-game leaderboard and persist stats."""
    entries = session.get_leaderboard()

    if not entries:
        _bot.send_message(group_id, "🎮 Game over! No scores to show.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines  = ["🏆 <b>Final Leaderboard</b> 🏆\n"]

    winner_ids: list[int] = []

    for i, (uid_or_tid, info) in enumerate(entries):
        medal = medals[i] if i < 3 else f"  {i + 1}."
        pts   = info["points"]
        lines.append(f"{medal} <b>{info['name']}</b> — {pts} pts")
        if i == 0:
            # Collect winner user IDs
            if session.mode == "team":
                winner_ids = info["members"]
            else:
                winner_ids = [uid_or_tid]

    _bot.send_message(group_id, "\n".join(lines))

    # ── Persist stats ──────────────────────────────────────────────────
    for uid, pinfo in session.players.items():
        pts  = pinfo["points"]
        won  = uid in winner_ids
        db.record_game_result(uid, pts, won)


# ── Round / game flow ──────────────────────────────────────────────────────

def _advance(group_id: int) -> None:
    """Move to the next round, or end the game."""
    session = _get_session(group_id)
    if not session:
        return   # game was stopped externally

    if session.next_round():
        _send_word(group_id, session)
    else:
        _bot.send_message(group_id, "🎉 <b>Game Over!</b>")
        _send_final_leaderboard(group_id, session)
        active_games.pop(group_id, None)


def _handle_timeout(group_id: int) -> None:
    """Called by the round timer when time expires."""
    session = _get_session(group_id)
    if not session or session.state != GameSession.STATE_RUNNING:
        return

    if not session.claim_round():
        return  # a player already answered

    _send_round_result(group_id, session, winner=None)
    threading.Timer(2.0, _advance, args=(group_id,)).start()


# ══════════════════════════════════════════════════════════════════════════════
#  Handler registration
# ══════════════════════════════════════════════════════════════════════════════

def register(bot: telebot.TeleBot) -> None:
    global _bot
    _bot = bot

    # ── /start  /help ──────────────────────────────────────────────────────
    @bot.message_handler(commands=["start", "help"])
    def cmd_help(msg: Message):
        _bot.send_message(
            msg.chat.id,
            "🎓 <b>Synonym Game Bot</b>\n\n"
            "A vocabulary game for Stride English learners — guess the synonym!\n\n"
            "<b>Player Commands</b>\n"
            "▸ /startgame  — Start a solo game\n"
            "▸ /startteam  — Start a team game (join phase)\n"
            "▸ /join       — Join a pending team game\n"
            "▸ /begin      — Begin the team game (admin)\n"
            "▸ /hint       — Get a hint (costs pts)\n"
            "▸ /skip       — Skip the word (costs pts)\n"
            "▸ /leaderboard — Global leaderboard\n"
            "▸ /mystats    — Your personal stats\n\n"
            "<b>Admin Commands</b>\n"
            "▸ /activate        — Activate bot & set level\n"
            "▸ /settings        — View current settings\n"
            "▸ /setquestions [n] — Questions per game\n"
            "▸ /settime [n]     — Seconds per round\n"
            "▸ /togglehint      — Enable/disable hints\n"
            "▸ /toggleskip      — Enable/disable skip\n"
            "▸ /toggleapproval  — Require admin approval to start\n"
            "▸ /stopgame        — Force-stop active game\n"
            "▸ /ban [user_id]   — Ban a user\n"
            "▸ /unban [user_id] — Unban a user\n"
        )

    # ── /activate ──────────────────────────────────────────────────────────
    @bot.message_handler(commands=["activate"])
    def cmd_activate(msg: Message):
        if msg.chat.type not in ("group", "supergroup"):
            _bot.reply_to(msg, "⚠️ Use this command inside a group.")
            return
        if not _is_group_admin(msg.chat.id, msg.from_user.id):
            _bot.reply_to(msg, "🚫 Only group admins can activate the bot.")
            return
        _bot.send_message(
            msg.chat.id,
            "🎯 <b>Activate Synonym Game Bot</b>\n\nSelect the vocabulary level for this group:",
            reply_markup=_level_kbd()
        )

    # ── /startgame ─────────────────────────────────────────────────────────
    @bot.message_handler(commands=["startgame"])
    def cmd_startgame(msg: Message):
        if msg.chat.type not in ("group", "supergroup"):
            _bot.reply_to(msg, "⚠️ Use this command in a group.")
            return

        gid   = msg.chat.id
        group = db.get_group(gid)

        if not group or not group["activated"]:
            _bot.reply_to(msg, "❌ This group isn't activated yet. An admin must run /activate first.")
            return
        if db.is_banned(msg.from_user.id):
            _bot.reply_to(msg, "🚫 You are banned from playing.")
            return
        if gid in active_games:
            _bot.reply_to(msg, "⚠️ A game is already in progress! Use /stopgame first.")
            return

        settings = db.get_game_settings(gid)
        if settings.get("require_approval") and not _is_group_admin(gid, msg.from_user.id):
            _bot.reply_to(msg, "⏳ Admin approval is required to start a game. Ask an admin!")
            return

        # Register the initiator and mark setup in progress
        db.ensure_user(msg.from_user.id, msg.from_user.first_name)
        active_games[gid] = {
            "_setup":    True,
            "_mode":     "solo",
            "_initiator": msg.from_user.id,
        }
        level = group["level"]
        _bot.send_message(
            gid,
            f"🎮 <b>New Game</b>  |  Level: <b>{level}</b>\n\nPick a category:",
            reply_markup=_category_kbd(level)
        )

    # ── /startteam ─────────────────────────────────────────────────────────
    @bot.message_handler(commands=["startteam"])
    def cmd_startteam(msg: Message):
        if msg.chat.type not in ("group", "supergroup"):
            _bot.reply_to(msg, "⚠️ Use this command in a group.")
            return

        gid   = msg.chat.id
        group = db.get_group(gid)

        if not group or not group["activated"]:
            _bot.reply_to(msg, "❌ Group not activated. Admin must use /activate.")
            return
        if db.is_banned(msg.from_user.id):
            _bot.reply_to(msg, "🚫 You are banned from playing.")
            return
        if gid in active_games:
            _bot.reply_to(msg, "⚠️ A game is already running!")
            return

        uid  = msg.from_user.id
        name = msg.from_user.first_name
        db.ensure_user(uid, name)

        active_games[gid] = {
            "_setup":    True,
            "_joining":  True,
            "_mode":     "team",
            "_initiator": uid,
            "_joiners":  {uid: name},
        }

        _bot.send_message(
            gid,
            f"👥 <b>Team Mode — Join Phase!</b>\n"
            f"Level: <b>{group['level']}</b>\n\n"
            f"Players: type /join to enter.\n"
            f"Admin: type /begin when ready.\n\n"
            f"Currently joined:\n• {name}"
        )

    # ── /join ──────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["join"])
    def cmd_join(msg: Message):
        gid   = msg.chat.id
        state = active_games.get(gid)

        if not isinstance(state, dict) or not state.get("_joining"):
            _bot.reply_to(msg, "❌ No team game is currently accepting players.")
            return
        if db.is_banned(msg.from_user.id):
            _bot.reply_to(msg, "🚫 You are banned from playing.")
            return

        uid, name = msg.from_user.id, msg.from_user.first_name
        if uid in state["_joiners"]:
            _bot.reply_to(msg, "✅ You've already joined!")
            return

        state["_joiners"][uid] = name
        db.ensure_user(uid, name)

        player_list = "\n".join(f"• {n}" for n in state["_joiners"].values())
        _bot.send_message(gid, f"✅ <b>{name}</b> joined!\n\nPlayers ({len(state['_joiners'])}):\n{player_list}")

    # ── /begin ─────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["begin"])
    def cmd_begin(msg: Message):
        gid   = msg.chat.id
        state = active_games.get(gid)

        if not isinstance(state, dict) or not state.get("_joining"):
            _bot.reply_to(msg, "❌ No team game pending. Use /startteam first.")
            return
        if not _is_group_admin(gid, msg.from_user.id):
            _bot.reply_to(msg, "🚫 Only admins can begin the game.")
            return

        joiners = state["_joiners"]
        if len(joiners) < 2:
            _bot.reply_to(msg, "⚠️ Need at least 2 players to start a team game!")
            return

        # Close join phase; show category selection
        state["_joining"] = False
        group = db.get_group(gid)
        level = group["level"]

        _bot.send_message(
            gid,
            f"👥 <b>{len(joiners)} players locked in!</b>\n"
            f"Level: <b>{level}</b>\n\nNow pick a category:",
            reply_markup=_category_kbd(level)
        )

    # ── /hint ──────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["hint"])
    def cmd_hint(msg: Message):
        session = _get_session(msg.chat.id)
        if not session or session.state != GameSession.STATE_RUNNING:
            _bot.reply_to(msg, "❌ No active round right now.")
            return
        if not session.hints_enabled:
            _bot.reply_to(msg, "💡 Hints are disabled in this group.")
            return

        uid, name = msg.from_user.id, msg.from_user.first_name
        session.add_player(uid, name)   # auto-register if new
        session.deduct_points(uid, HINT_COST)
        hint = session.get_hint()
        _bot.send_message(
            msg.chat.id,
            f"💡 Hint for <b>{session.current_word['word']}</b>:  <b>{hint}</b>"
            f"\n(−{HINT_COST} pts from {name})"
        )

    # ── /skip ──────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["skip"])
    def cmd_skip(msg: Message):
        gid     = msg.chat.id
        session = _get_session(gid)
        if not session or session.state != GameSession.STATE_RUNNING:
            _bot.reply_to(msg, "❌ No active round right now.")
            return
        if not session.skip_enabled:
            _bot.reply_to(msg, "⏭️ Skipping is disabled in this group.")
            return

        uid, name = msg.from_user.id, msg.from_user.first_name
        if not session.claim_round():
            return   # already claimed (someone answered simultaneously)

        session.cancel_timer()
        session.add_player(uid, name)
        session.deduct_points(uid, SKIP_COST)

        _bot.send_message(gid, f"⏭️ <b>{name}</b> skipped the word. (−{SKIP_COST} pt)")
        _send_round_result(gid, session, winner=None)
        threading.Timer(2.0, _advance, args=(gid,)).start()

    # ── /stopgame ──────────────────────────────────────────────────────────
    @bot.message_handler(commands=["stopgame"])
    def cmd_stopgame(msg: Message):
        gid = msg.chat.id
        if not _is_group_admin(gid, msg.from_user.id):
            _bot.reply_to(msg, "🚫 Admins only.")
            return

        obj = active_games.get(gid)
        if not obj:
            _bot.reply_to(msg, "No active game to stop.")
            return

        if isinstance(obj, GameSession):
            obj.cancel_timer()
        active_games.pop(gid, None)
        _bot.send_message(gid, "🛑 Game stopped by admin.")

    # ── /leaderboard ───────────────────────────────────────────────────────
    @bot.message_handler(commands=["leaderboard"])
    def cmd_leaderboard(msg: Message):
        entries = db.get_global_leaderboard(10)
        if not entries:
            _bot.reply_to(msg, "📊 No scores yet — be the first to play!")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines  = ["🌍 <b>Global Leaderboard</b>\n"]
        for i, e in enumerate(entries):
            medal = medals[i] if i < 3 else f"  {i + 1}."
            name  = e.get("username") or f"User {e['user_id']}"
            lines.append(
                f"{medal} <b>{name}</b> — {e['total_points']} pts"
                f"  (W:{e['wins']} L:{e['losses']})"
            )
        _bot.send_message(msg.chat.id, "\n".join(lines))

    # ── /mystats ───────────────────────────────────────────────────────────
    @bot.message_handler(commands=["mystats"])
    def cmd_mystats(msg: Message):
        uid = msg.from_user.id
        db.ensure_user(uid, msg.from_user.first_name)
        user = db.get_user(uid)
        if not user:
            _bot.reply_to(msg, "No stats yet — go play!")
            return
        _bot.reply_to(
            msg,
            f"📊 <b>Your Stats — {msg.from_user.first_name}</b>\n\n"
            f"⭐ Total Points: <b>{user['total_points']}</b>\n"
            f"🏆 Wins:         <b>{user['wins']}</b>\n"
            f"😔 Losses:       <b>{user['losses']}</b>\n"
            f"🚫 Banned:       {'Yes' if user['is_banned'] else 'No'}"
        )

    # ── /settings ──────────────────────────────────────────────────────────
    @bot.message_handler(commands=["settings"])
    def cmd_settings(msg: Message):
        gid = msg.chat.id
        if msg.chat.type not in ("group", "supergroup"):
            _bot.reply_to(msg, "⚠️ Use in a group.")
            return
        if not _is_group_admin(gid, msg.from_user.id):
            _bot.reply_to(msg, "🚫 Admins only.")
            return

        group = db.get_group(gid)
        level = group["level"] if group else "N/A"
        s     = db.get_game_settings(gid)

        _bot.send_message(
            gid,
            f"⚙️ <b>Group Settings</b>\n\n"
            f"📚 Level:            <b>{level}</b>  (/activate to change)\n"
            f"❓ Questions/game:   <b>{s['num_questions']}</b>  — /setquestions [n]\n"
            f"⏱ Seconds/round:    <b>{s['time_per_round']}s</b>  — /settime [n]\n"
            f"💡 Hints:            <b>{'On' if s['hints_enabled'] else 'Off'}</b>  — /togglehint\n"
            f"⏭ Skip:             <b>{'On' if s['skip_enabled'] else 'Off'}</b>  — /toggleskip\n"
            f"🔐 Require approval: <b>{'Yes' if s['require_approval'] else 'No'}</b>  — /toggleapproval\n"
        )

    # Settings mutators ──────────────────────────────────────────────────────

    @bot.message_handler(commands=["setquestions"])
    def cmd_setquestions(msg: Message):
        gid = msg.chat.id
        if not _is_group_admin(gid, msg.from_user.id):
            _bot.reply_to(msg, "🚫 Admins only.")
            return
        parts = msg.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            _bot.reply_to(msg, "Usage: /setquestions [number]   e.g. /setquestions 10")
            return
        n = int(parts[1])
        if not 1 <= n <= 50:
            _bot.reply_to(msg, "Please pick a number between 1 and 50.")
            return
        db.update_game_settings(gid, num_questions=n)
        _bot.reply_to(msg, f"✅ Questions per game set to <b>{n}</b>.")

    @bot.message_handler(commands=["settime"])
    def cmd_settime(msg: Message):
        gid = msg.chat.id
        if not _is_group_admin(gid, msg.from_user.id):
            _bot.reply_to(msg, "🚫 Admins only.")
            return
        parts = msg.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            _bot.reply_to(msg, "Usage: /settime [seconds]   e.g. /settime 60")
            return
        n = int(parts[1])
        if not 10 <= n <= 300:
            _bot.reply_to(msg, "Please pick a value between 10 and 300 seconds.")
            return
        db.update_game_settings(gid, time_per_round=n)
        _bot.reply_to(msg, f"✅ Time per round set to <b>{n}s</b>.")

    @bot.message_handler(commands=["togglehint"])
    def cmd_togglehint(msg: Message):
        gid = msg.chat.id
        if not _is_group_admin(gid, msg.from_user.id):
            _bot.reply_to(msg, "🚫 Admins only.")
            return
        s   = db.get_game_settings(gid)
        new = 0 if s["hints_enabled"] else 1
        db.update_game_settings(gid, hints_enabled=new)
        _bot.reply_to(msg, f"💡 Hints are now <b>{'enabled' if new else 'disabled'}</b>.")

    @bot.message_handler(commands=["toggleskip"])
    def cmd_toggleskip(msg: Message):
        gid = msg.chat.id
        if not _is_group_admin(gid, msg.from_user.id):
            _bot.reply_to(msg, "🚫 Admins only.")
            return
        s   = db.get_game_settings(gid)
        new = 0 if s["skip_enabled"] else 1
        db.update_game_settings(gid, skip_enabled=new)
        _bot.reply_to(msg, f"⏭️ Skip is now <b>{'enabled' if new else 'disabled'}</b>.")

    @bot.message_handler(commands=["toggleapproval"])
    def cmd_toggleapproval(msg: Message):
        gid = msg.chat.id
        if not _is_group_admin(gid, msg.from_user.id):
            _bot.reply_to(msg, "🚫 Admins only.")
            return
        s   = db.get_game_settings(gid)
        new = 0 if s.get("require_approval") else 1
        db.update_game_settings(gid, require_approval=new)
        _bot.reply_to(
            msg,
            f"🔐 Admin approval to start games is now "
            f"<b>{'required' if new else 'not required'}</b>."
        )

    # ── /ban  /unban ───────────────────────────────────────────────────────
    @bot.message_handler(commands=["ban"])
    def cmd_ban(msg: Message):
        if not _is_group_admin(msg.chat.id, msg.from_user.id) and \
           not _is_super_admin(msg.from_user.id):
            _bot.reply_to(msg, "🚫 Admins only.")
            return
        parts = msg.text.split()
        if len(parts) < 2:
            _bot.reply_to(msg, "Usage: /ban [user_id]")
            return
        try:
            target = int(parts[1])
        except ValueError:
            _bot.reply_to(msg, "⚠️ Invalid user ID.")
            return
        db.ban_user(target)
        _bot.reply_to(msg, f"🔨 User <code>{target}</code> has been banned.")

    @bot.message_handler(commands=["unban"])
    def cmd_unban(msg: Message):
        if not _is_group_admin(msg.chat.id, msg.from_user.id) and \
           not _is_super_admin(msg.from_user.id):
            _bot.reply_to(msg, "🚫 Admins only.")
            return
        parts = msg.text.split()
        if len(parts) < 2:
            _bot.reply_to(msg, "Usage: /unban [user_id]")
            return
        try:
            target = int(parts[1])
        except ValueError:
            _bot.reply_to(msg, "⚠️ Invalid user ID.")
            return
        db.unban_user(target)
        _bot.reply_to(msg, f"✅ User <code>{target}</code> has been unbanned.")

    # ══════════════════════════════════════════════════════════════════════
    #  Answer handler — fires on every group text message
    # ══════════════════════════════════════════════════════════════════════
    @bot.message_handler(
        func=lambda m: m.chat.type in ("group", "supergroup") and m.text
    )
    def handle_answer(msg: Message):
        gid     = msg.chat.id
        session = _get_session(gid)

        if not session or session.state != GameSession.STATE_RUNNING:
            return

        uid, name = msg.from_user.id, msg.from_user.first_name
        if db.is_banned(uid):
            return

        if not session.check_answer(msg.text):
            return   # wrong answer — keep waiting

        # Thread-safe claim: first correct answer wins the round
        if not session.claim_round():
            return   # another answer arrived at the same time

        session.cancel_timer()
        session.add_player(uid, name)        # auto-register if new joiner
        session.award_points(uid, CORRECT_POINTS)
        db.ensure_user(uid, name)

        _send_round_result(gid, session, winner=name)
        threading.Timer(2.0, _advance, args=(gid,)).start()

    # ══════════════════════════════════════════════════════════════════════
    #  Callback query handler — inline keyboard responses
    # ══════════════════════════════════════════════════════════════════════
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callback(call: CallbackQuery):
        data = call.data
        gid  = call.message.chat.id
        uid  = call.from_user.id

        # ── Level selection (group activation) ─────────────────────────
        if data.startswith("act_level:"):
            level = data.split(":")[1]
            if not _is_group_admin(gid, uid):
                _bot.answer_callback_query(call.id, "🚫 Only admins can do this.")
                return
            db.activate_group(gid, level, uid)
            _bot.edit_message_text(
                f"✅ <b>Group activated!</b>\n\n"
                f"Vocabulary level: <b>{level}</b>\n"
                f"Word count: <b>{word_cache.word_count(level)}</b>\n\n"
                f"Players can start with /startgame\n"
                f"Adjust settings with /settings",
                chat_id=gid,
                message_id=call.message.message_id
            )
            _bot.answer_callback_query(call.id, f"✅ Activated at level {level}!")

        # ── Category selection ──────────────────────────────────────────
        elif data.startswith("cat:"):
            category = data[4:]
            state    = active_games.get(gid)

            if not isinstance(state, dict) or not state.get("_setup"):
                _bot.answer_callback_query(call.id, "No pending game found.")
                return

            # Only the initiator picks the category
            if uid != state["_initiator"]:
                _bot.answer_callback_query(
                    call.id, "Only the person who started the game can pick!"
                )
                return

            group    = db.get_group(gid)
            level    = group["level"]
            settings = db.get_game_settings(gid)
            words    = word_cache.get_words(level, category)

            if not words:
                _bot.answer_callback_query(
                    call.id, f"No words found for {level} / {category}!"
                )
                return

            mode    = state["_mode"]
            joiners = state.get("_joiners", {uid: call.from_user.first_name})
            session = GameSession(
                group_id=gid,
                mode=mode,
                level=level,
                category=category,
                words=words,
                settings=settings,
            )

            # Register all players
            for join_uid, join_name in joiners.items():
                session.add_player(join_uid, join_name)

            if mode == "team":
                session.assign_teams_random()

            active_games[gid] = session

            # Build announcement
            mode_label = "👥 Team Mode" if mode == "team" else "👤 Solo Mode"
            _bot.edit_message_text(
                f"🎮 <b>Game Starting!</b>\n\n"
                f"Mode:     {mode_label}\n"
                f"Level:    <b>{level}</b>\n"
                f"Category: <b>{category}</b>\n"
                f"Words:    <b>{session.total_rounds}</b>\n\n"
                f"Get ready...",
                chat_id=gid,
                message_id=call.message.message_id
            )

            if mode == "team" and session.teams:
                team_lines = "\n".join(f"• {t['name']}" for t in session.teams.values())
                _bot.send_message(gid, f"👥 <b>Teams for this game:</b>\n{team_lines}")

            _bot.answer_callback_query(call.id, "Starting!")
            threading.Timer(2.5, _start_first_round, args=(gid,)).start()

        else:
            _bot.answer_callback_query(call.id)


def _start_first_round(group_id: int) -> None:
    """Delayed kick-off so players can read the game announcement first."""
    session = _get_session(group_id)
    if not session:
        return
    if session.next_round():
        _send_word(group_id, session)