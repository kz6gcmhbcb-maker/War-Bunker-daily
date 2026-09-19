# War Bunker — Daily Raid Tracker

A clean Discord bot for WChronicles raid monitoring.

## Commands

- `/setup` — configure a channel and tracked faction
- `/daily` — attacked + not attacked today
- `/daily view:attacked` — only observed attackers
- `/daily view:missing` — only players with no observed attack
- `/daily faction:<faction>` — inspect another faction
- `/status`
- `/update`
- `/top5`
- `/topfactions`
- `/stats`
- `/enable`
- `/disable`

## Daily attack logic

The currently available leaderboard endpoint exposes cumulative `attacks` for each player. It does not expose a documented per-attack timestamp/history in the data available to this project.

War Bunker therefore watches the cumulative attack counter. When it increases, the bot records the delta for that player on the current day.

This gives reliable **observed attacks while the bot is running**.

If War Bunker starts after a player has already attacked that day, the earlier attack cannot be reconstructed from the leaderboard endpoint alone. The `/daily` embed explicitly shows the tracker start time so the result is not misleading.

## Persistence on Railway

The daily database is SQLite.

For persistence across Railway restarts, attach a Railway Volume and set:

```text
DB_FILE=/data/war_bunker.sqlite3
```

Without persistent storage, the daily history can be lost when the service is redeployed/restarted.

## Environment variables

```text
DISCORD_TOKEN=
POLL_SECONDS=60
DAILY_TIMEZONE=Europe/Sofia
DB_FILE=war_bunker.sqlite3
API_URL=https://chronicles.wfitapp.xyz/api/raid/leaderboard?limit=25
```

Never commit the Discord token.

## Run locally

```bash
pip install -r requirements.txt
python bot.py
```

## Railway

Set `DISCORD_TOKEN` in Railway Variables.

Optional:

```text
POLL_SECONDS=60
DAILY_TIMEZONE=Europe/Sofia
DB_FILE=/data/war_bunker.sqlite3
```
