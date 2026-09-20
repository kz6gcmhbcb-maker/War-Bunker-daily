# War Bunker — Daily Raid Tracker v1.2

Discord bot for WChronicles raid monitoring with persistent daily history.

## Commands

- `/setup` — configure channel and tracked faction
- `/daily` — show all captured roster members for today
- `/daily view:attacked` — only players with observed attacks
- `/daily view:missing` — players with no observed attack
- `/daily faction:<faction>` — inspect another faction
- `/daily date:YYYY-MM-DD` — open a saved historical day
- `/dailyhistory` — list recent saved days
- `/status`
- `/update`
- `/top5`
- `/topfactions`
- `/stats`
- `/enable`
- `/disable`

## Important v1.2 changes

### Full faction roster
The default leaderboard URL now requests `limit=1000` instead of `limit=25` so `/daily` can capture faction members that are outside the first 25 global leaderboard entries.

If the WChronicles API itself hard-caps the response at 25 entries, the bot cannot discover the remaining players from this endpoint alone; in that case another roster/pagination endpoint is required.

### Attack timestamps
The leaderboard exposes a cumulative `attacks` counter, not an official per-attack timestamp/history. When the bot notices the counter increase, it stores the current bot time as a **detection timestamp**.

Therefore:
- `🕒 20:14:33` means the bot detected the new attack at that time.
- It is not guaranteed to be the exact in-game attack time.
- An attack that happened before the bot first saw that player cannot be reconstructed from the leaderboard alone.

### Historical days
Every tracked day is stored in SQLite.

Examples:

```text
/daily date:2026-09-19
/daily date:2026-09-18 view:missing
/dailyhistory days:7
```

Historical `/daily` views are read-only. Today's `/daily` refreshes the live API and records the current roster before displaying it.

## Railway persistence

Attach a Railway Volume and set:

```text
DB_FILE=/data/war_bunker.sqlite3
```

Without persistent storage, daily history can disappear after a redeploy/restart.

## Environment variables

```text
DISCORD_TOKEN=
POLL_SECONDS=60
DAILY_TIMEZONE=Europe/Sofia
DB_FILE=/data/war_bunker.sqlite3
API_URL=https://chronicles.wfitapp.xyz/api/raid/leaderboard?limit=1000
```

Never commit the Discord token.

## Run locally

```bash
pip install -r requirements.txt
python bot.py
```
