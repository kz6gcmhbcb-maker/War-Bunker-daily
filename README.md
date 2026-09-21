# WChronicles RaidOps v1.5

Discord raid monitoring and daily attack tracker for WChronicles.

## v1.5 defaults

- Bot name: `WChronicles RaidOps`
- Version: `1.5.0`
- API limit: `60`
- Timezone: `UTC`
- Poll interval: `60` seconds by default
- SQLite database: configure `DB_FILE`; on Railway use `/data/war_bunker.sqlite3`
- Automatic alerts are event-driven: no message when nothing changed.
- Daily attack history is stored in SQLite.
- Attack times are **bot detection timestamps**, because the leaderboard endpoint does not expose official per-attack timestamps.

## Railway variables

Set:

```text
DISCORD_TOKEN=your_token
POLL_SECONDS=60
API_URL=https://chronicles.wfitapp.xyz/api/raid/leaderboard?limit=60
DB_FILE=/data/war_bunker.sqlite3
```

### Important: persistent history

For history to survive Railway redeploys/restarts, create a Railway Volume and mount it at:

```text
/data
```

Then keep:

```text
DB_FILE=/data/war_bunker.sqlite3
```

Without a persistent volume, the bot can run but SQLite history may be lost when the service filesystem is replaced.

## Commands

- `/setup` — choose the automatic channel and faction
- `/status` — configuration, API limit, UTC, DB status/path
- `/daily` — today's daily attack check
  - `view: all`
  - `view: attacked`
  - `view: missing`
  - optional `date: YYYY-MM-DD`
- `/dailyhistory` — recent recorded days
- `/activity` — latest recorded attacks for a faction
- `/update` — manual live faction leaderboard
- `/top5`
- `/top10`
- `/topfactions`
- `/raid`
- `/stats`
- `/gap`
- `/intel`
- `/enable` / `/disable` — automatic attack alerts

## Important roster note

`/daily` can show members with 0 attacks only when those members are present in the API response. With the configured `limit=60`, the API must return the faction member for the bot to know that member exists. If the API itself omits a member, the bot cannot reconstruct that member from this endpoint alone.

## Deployment

The service should start with:

```text
python bot.py
```

A `Procfile` is included for platforms that use one.
