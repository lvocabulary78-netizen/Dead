# 🎓 Synonym Game Bot

A multiplayer English vocabulary bot for Telegram groups — built for your batch!  
Players race to guess synonyms, earn points, and climb the leaderboard.

---

## Files

```
bot.py          ← entry point (run this)
config.py       ← your token + super-admin IDs
db.py           ← SQLite layer (groups, users, settings)
word_cache.py   ← loads words.json into RAM at startup
game_logic.py   ← GameSession class (all in-memory game state)
handlers.py     ← every Telegram command & callback
words.json      ← your vocabulary dataset
requirements.txt
```

---

## Quick Setup

### 1. Install the library
```bash
pip install pyTelegramBotAPI
```
*(On Pydroid3: open the terminal tab → `pip install pyTelegramBotAPI`)*

### 2. Put your bot token in config.py
```python
BOT_TOKEN = "your_token_from_BotFather"
```
Or set an environment variable:
```bash
export BOT_TOKEN="your_token"
```

### 3. (Optional) Add your Telegram user ID as a super-admin
```python
SUPER_ADMIN_IDS = [123456789]
```

### 4. Make sure words.json is in the same folder as bot.py

### 5. Run
```bash
python bot.py
```

---

## Group Setup (do this once per group)

1. Add the bot to your Telegram group and **make it an admin**.
2. A group admin types `/activate` and selects the vocabulary level (A1 / A2 / B1).
3. Done! The group is now locked to that level's word pool.

---

## Game Flow

### Solo Mode (`/startgame`)
```
Admin or player → /startgame
Bot shows category keyboard → player picks one
Bot sends first word → players type synonyms
First correct answer wins the round (+5 pts)
Round ends → bot reveals synonyms, Arabic translation, example sentence
Repeat for all rounds → final leaderboard shown
```

### Team Mode (`/startteam` → `/join` → `/begin`)
```
Admin → /startteam        (opens join phase)
Players → /join           (each player joins)
Admin → /begin            (closes join phase)
Bot shows category keyboard → initiator picks
Bot randomly pairs players into teams of 2
Game runs like solo, but points go to the team
Final leaderboard shows team rankings
```

---

## All Commands

### Player commands
| Command | What it does |
|---|---|
| `/startgame` | Start a solo game in this group |
| `/startteam` | Open the team join phase |
| `/join` | Join a pending team game |
| `/hint` | Reveal first letter of a synonym (−2 pts) |
| `/skip` | Skip the current word (−1 pt) |
| `/leaderboard` | Global top-10 by total points |
| `/mystats` | Your personal wins / losses / points |
| `/help` | Command list |

### Admin commands (group admin or super-admin)
| Command | What it does |
|---|---|
| `/activate` | Activate bot & set vocabulary level |
| `/begin` | Start a team game after join phase |
| `/stopgame` | Force-stop the active game |
| `/settings` | View all current settings |
| `/setquestions [n]` | Set questions per game (1–50) |
| `/settime [n]` | Set seconds per round (10–300) |
| `/togglehint` | Enable / disable hints |
| `/toggleskip` | Enable / disable skip |
| `/toggleapproval` | Require admin approval to start |
| `/ban [user_id]` | Ban a user from playing |
| `/unban [user_id]` | Unban a user |

---

## Scoring

| Event | Points |
|---|---|
| Correct answer | +5 |
| Using /hint | −2 (session only) |
| Using /skip | −1 (session only) |

**Note:** hint/skip costs are deducted from the *session score only* — not from the persistent `total_points` in the database.

---

## Architecture Notes

- **words.json is loaded once** into a nested dict `{level → {category → [entries]}}` — zero DB queries during gameplay.
- **SQLite** stores only persistent data: activated groups, game settings, user stats.
- **GameSession** is a pure in-memory object. Multiple groups can run independent games simultaneously with no shared state.
- **Thread safety**: `claim_round()` uses a `threading.Lock` to prevent a correct answer and a timeout from both advancing the round.
- **WAL mode** is enabled on SQLite for concurrent read safety.

---

## Adding / Editing Words

The vocabulary lives entirely in **words.json**. Each entry:
```json
{
  "word": "happy",
  "synonyms": ["glad", "joyful"],
  "arabic": "سعيد",
  "example": "She is happy with her gift.",
  "level": "A1",
  "category": "Emotions"
}
```
Edit the file and restart the bot — changes load automatically on startup.
