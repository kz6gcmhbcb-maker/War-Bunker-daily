# WChronicles RaidOps v1.5.1

## 6-channel / 6-faction tracking

One Discord server supports **up to 6 independent trackers**. Example:

- Neon Hive → `#test1`
- El Immortals → `#test2`
- Faction 3 → `#test3`
- Faction 4 → `#test4`
- Faction 5 → `#test5`
- Faction 6 → `#test6`

Each faction is stored by `(guild_id, faction)`, so setting a new faction **does not overwrite** the previous one.

The monitor makes one API request and then routes each detected faction's events to its own configured channel.

## Setup

Run `/setup` six times:

`/setup channel:#test1 faction:Neon Hive`

`/setup channel:#test2 faction:El Immortals`

…and so on for the other four factions.

`/status` shows all six mappings.

## Important

- API limit: 60
- Poll: 60 seconds
- Timezone: UTC
- Persistent DB: `/data/war_bunker.sqlite3`
- Railway Volume should be mounted at `/data`.
- Existing old `guild_config` data is migrated automatically.
- Maximum 6 trackers per Discord server.
- Daily counts are based on increases observed by the API poll; detection time is the bot's detection time.

## Railway variables

DISCORD_TOKEN=...
API_URL=https://chronicles.wfitapp.xyz/api/raid/leaderboard?limit=60
POLL_SECONDS=60
DAILY_TIMEZONE=UTC
DB_FILE=/data/war_bunker.sqlite3

No Message Content Intent is required; the bot uses slash commands.
