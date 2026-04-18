"""
game_logic.py — Self-contained GameSession class.

Handles all in-memory game state: rounds, scoring, teams, timers.
No I/O — the handlers layer is responsible for sending Telegram messages
and persisting results to the database.
"""

import random
import threading
import re
import logging
from difflib import SequenceMatcher
from config import CORRECT_POINTS, HINT_COST, SKIP_COST, DEFAULT_NUM_QUESTIONS, DEFAULT_TIME_PER_ROUND

logger = logging.getLogger(__name__)

# Try to import fast Levenshtein, fallback to difflib
try:
    from Levenshtein import ratio as levenshtein_ratio
except ImportError:
    logger.warning("python-Levenshtein not installed; using difflib (slower).")
    def levenshtein_ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()


class GameSession:
    """
    Represents one active game in a single Telegram group.

    Lifecycle:
        WAITING ──(next_round)──► RUNNING ──(all rounds done)──► FINISHED
    """

    STATE_WAITING  = "waiting"   # session created, first round not yet started
    STATE_RUNNING  = "running"   # a round is live
    STATE_FINISHED = "finished"  # all rounds complete

    # ── Construction ──────────────────────────────────────────────────────

    def __init__(
        self,
        group_id:  int,
        mode:      str,       # "solo" | "team"
        level:     str,
        category:  str,
        words:     list[dict],
        settings:  dict,
    ) -> None:
        self.group_id = group_id
        self.mode     = mode
        self.level    = level
        self.category = category
        self.state    = self.STATE_WAITING

        # ── Settings ──────────────────────────────────────────────────────
        n = settings.get("num_questions", DEFAULT_NUM_QUESTIONS)
        self.num_questions    = min(n, len(words))
        self.time_per_round   = settings.get("time_per_round", DEFAULT_TIME_PER_ROUND)
        self.hints_enabled    = bool(settings.get("hints_enabled", 1))
        self.skip_enabled     = bool(settings.get("skip_enabled", 1))

        # ── Word list ─────────────────────────────────────────────────────
        self._words: list[dict] = random.sample(words, self.num_questions)
        self._index: int = -1          # incremented before first use
        self.current_word: dict | None = None

        # ── Players  {user_id: {"name": str, "points": int}} ──────────────
        self.players: dict[int, dict] = {}

        # ── Teams  {team_id: {"name": str, "members": list[int], "points": int}} ──
        self.teams: dict[str, dict] = {}

        # ── Round state ────────────────────────────────────────────────────
        self._round_lock = threading.Lock()
        self._round_claimed = False    # True once someone (player or timer) claims the round

        # ── Timer ─────────────────────────────────────────────────────────
        self._timer: threading.Timer | None = None

    # ── Player / Team management ─────────────────────────────────────────

    def add_player(self, user_id: int, name: str) -> None:
        if user_id not in self.players:
            self.players[user_id] = {"name": name, "points": 0}

    def remove_player(self, user_id: int) -> None:
        self.players.pop(user_id, None)

    def assign_teams_random(self) -> None:
        """
        Randomly pair players into teams of 2.
        If an odd number of players, the last team has 3 members.
        """
        ids = list(self.players.keys())
        random.shuffle(ids)
        self.teams.clear()
        team_num = 1
        for i in range(0, len(ids), 2):
            members = ids[i : i + 2]
            names   = [self.players[uid]["name"] for uid in members]
            team_id = f"team_{team_num}"
            self.teams[team_id] = {
                "name":    f"Team {team_num} ({' & '.join(names)})",
                "members": members,
                "points":  0,
            }
            team_num += 1

    def get_player_team(self, user_id: int) -> str | None:
        for tid, team in self.teams.items():
            if user_id in team["members"]:
                return tid
        return None

    # ── Round control ─────────────────────────────────────────────────────

    def next_round(self) -> bool:
        """
        Advance to the next word.
        Returns True if a new round started, False if the game is over.
        """
        self._index += 1
        if self._index >= len(self._words):
            self.state = self.STATE_FINISHED
            return False
        self.current_word   = self._words[self._index]
        self.state          = self.STATE_RUNNING
        self._round_claimed = False
        return True

    def claim_round(self) -> bool:
        """
        Thread-safe claim: first caller (player answer OR timeout) wins.
        Returns True if this call successfully claimed the round.
        """
        with self._round_lock:
            if self._round_claimed:
                return False
            self._round_claimed = True
            return True

    # ── Answer validation ─────────────────────────────────────────────────

    def check_answer(self, answer: str) -> bool:
        """Case‑insensitive, punctuation‑stripped, typo‑tolerant synonym check."""
        if not self.current_word:
            return False

        # Normalize input: lowercase, strip, remove punctuation
        cleaned = re.sub(r"[^\w\s]", "", answer.lower().strip())
        if not cleaned:
            return False

        # Get valid synonyms, also clean them
        valid_synonyms = []
        for s in self.current_word["synonyms"]:
            s_clean = re.sub(r"[^\w\s]", "", s.lower().strip())
            if s_clean:
                valid_synonyms.append(s_clean)

        # First try exact cleaned match
        if cleaned in valid_synonyms:
            return True

        # Fuzzy match: allow up to 2 edit distance or 85% similarity
        for syn in valid_synonyms:
            # Quick check: length difference too big → skip
            if abs(len(cleaned) - len(syn)) > 2:
                continue
            ratio = levenshtein_ratio(cleaned, syn)
            if ratio >= 0.75:   # 85% similarity (allows 1-2 typos)
                logger.info(
                    f"Fuzzy match accepted: '{answer}' -> '{syn}' (ratio: {ratio:.2f})"
                )
                return True

        return False

    # ── Scoring ───────────────────────────────────────────────────────────

    def award_points(self, user_id: int, points: int) -> None:
        """Add points to the player (and their team in team mode)."""
        if user_id in self.players:
            self.players[user_id]["points"] += points
        if self.mode == "team":
            tid = self.get_player_team(user_id)
            if tid and tid in self.teams:
                self.teams[tid]["points"] += points

    def deduct_points(self, user_id: int, points: int) -> None:
        """Deduct session points (hint/skip cost). Floor at 0."""
        if user_id in self.players:
            self.players[user_id]["points"] = max(
                0, self.players[user_id]["points"] - points
            )
        # Also deduct from team in team mode
        if self.mode == "team":
            tid = self.get_player_team(user_id)
            if tid and tid in self.teams:
                self.teams[tid]["points"] = max(
                    0, self.teams[tid]["points"] - points
                )

    def get_leaderboard(self) -> list[tuple]:
        """
        Returns sorted leaderboard entries.
        Solo  → sorted list of (user_id, player_dict)
        Team  → sorted list of (team_id, team_dict)
        """
        if self.mode == "team":
            return sorted(
                self.teams.items(),
                key=lambda x: x[1]["points"],
                reverse=True
            )
        return sorted(
            self.players.items(),
            key=lambda x: x[1]["points"],
            reverse=True
        )

    # ── Hint helper ───────────────────────────────────────────────────────

    def get_hint(self) -> str | None:
        """First letter of the first synonym + underscores for the rest."""
        if not self.current_word or not self.current_word["synonyms"]:
            return None
        syn = self.current_word["synonyms"][0]
        return syn[0].upper() + " _ " * (len(syn) - 1)

    # ── Timer ─────────────────────────────────────────────────────────────

    def start_timer(self, on_expire) -> None:
        """Start (or restart) the round timer."""
        self.cancel_timer()
        self._timer = threading.Timer(self.time_per_round, on_expire)
        self._timer.daemon = True
        self._timer.start()

    def cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    # ── Convenience properties ────────────────────────────────────────────

    @property
    def round_number(self) -> int:
        return self._index + 1

    @property
    def total_rounds(self) -> int:
        return len(self._words)

    def __repr__(self) -> str:
        return (
            f"<GameSession group={self.group_id} mode={self.mode} "
            f"level={self.level} cat={self.category} "
            f"round={self.round_number}/{self.total_rounds} state={self.state}>"
        )