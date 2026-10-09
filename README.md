# WChronicles RaidOps v2.0.0

Discord raid tracking for WChronicles factions, powered by the Chronicles leaderboard API. Supports **up to six faction trackers per Discord server**, automatic attack reports, stored daily observations, and personal notification preferences.

## Quick setup

Invite the bot with the `bot` and `applications.commands` scopes. In each tracker channel, give it **View Channel**, **Send Messages**, and **Embed Links**. Administrator and Message Content Intent are not required.

Members configuring trackers need **Manage Server** permission.

```text
/setup channel:#electric-immortals faction:Electric Immortals
/setup channel:#neon-hive faction:Neon Hive
/status
```

Each faction has its own channel mapping within a server. Setting another faction does not overwrite existing mappings. Running `/setup` again for the same faction updates its mapping and enables automatic alerts.

## Personal notifications

Attack reports remain visible in the configured channels. Each member can choose whether the bot mentions them when an attack is detected:

- `/silent mode:off` — subscribe to personal attack pings.
- `/silent mode:on` — stop personal attack pings.

**Personal pings start off until a member subscribes.** Preferences apply to all enabled trackers in that server, are independent between servers, and survive redeploys. The command responds privately and does not require Manage Server.

Set each raid channel's **Notification Settings → Only @mentions** so ordinary channel messages do not trigger notifications. The bot cannot change a member's Discord notification settings. Sound and push delivery remain subject to those settings.

Personal preferences do not stop tracking or public attack reports. `/enable` and `/disable` control the server's automatic reports; `/silent` controls only the requesting member's pings. Manual leaderboard posts do not ping subscribers. The bot does not mention roles or `@everyone`.

## Commands

| Command | Purpose |
| --- | --- |
| `/setup` | Add or update a faction/channel tracker; maximum six per server. |
| `/status` | Show mappings, timezone, snapshot freshness, poll errors, counter regressions, and queued alerts. |
| `/silent` | Subscribe to or stop your personal attack pings in this server. |
| `/daily` | Show observed player attack counts; filter all, attacked, or missing, with an optional date. |
| `/dailyhistory` | Show up to 30 stored observation dates and the number of players with detected increases for configured factions. |
| `/activity` | Show up to 20 recent detected attack increases for today. |
| `/update` | Post current faction standings to a configured channel. |
| `/top5`, `/top10` | Show the visible top players, optionally filtered by faction. |
| `/topfactions` | Show faction standings. |
| `/raid` | Show the current raid overview. |
| `/stats`, `/gap`, `/intel` | Show faction metrics, target gaps, and a tactical overview. |
| `/enable`, `/disable` | Enable or disable automatic reports for one or all server trackers. |
| `/removetracker` | Remove a faction mapping and its queued reports. |

`/setup`, `/update`, `/enable`, `/disable`, and `/removetracker` require Manage Server. The remaining commands are available to members with permission to use application commands.

For commands that accept a faction, choose it explicitly or use the uniquely configured tracker in the current channel. If the server has only one tracker, that tracker can be selected automatically. Ambiguous mappings require a faction selection.

## How daily history works

The API supplies cumulative raid counters. The bot records **increases between observations**:

- The first sighting of a player in a raid establishes a baseline; earlier attacks are not counted as new activity.
- New increases are stored in SQLite and assigned to the date when detected, using `DAILY_TIMEZONE`.
- `/daily` shows recorded attack counts per player.
- `/dailyhistory` counts **players with observed increases**, not total attacks. For example, `2/10` means two of ten observed players had recorded increases; those two players may have made many attacks.
- History builds from the first v2 observation. Earlier attacks cannot be assigned to dates from cumulative counters alone.
- Offline intervals and intervals spanning midnight are credited on detection. Activity timestamps are displayed in UTC; daily dates use the configured timezone.

The leaderboard may omit participants even when its requested limit is increased. **No observed increase does not prove inactivity.** Known players are retained across incomplete snapshots, but attacks from omitted players may be detected later when they reappear. Poll failures preserve the existing counters. A new raid establishes new baselines; decreases within the same raid retain the previous highest counter to avoid recounting attacks.

Legacy configuration is migrated automatically. Original history tables are retained; historical dates without v2 observations may appear as **legacy, unverified**. Legacy counts are not merged into v2 counts.

## Railway deployment

Keep the repository structure intact:

```text
bot.py
Dockerfile
requirements.txt
raidops/
  __init__.py
  api.py
  core.py
  views.py
```

Run one persistent worker using the root Dockerfile and `python bot.py`. No public domain, HTTP port, or HTTP healthcheck is required. Keep cron scheduling and serverless/sleeping mode disabled. Do not run another worker with the same production bot token.

Mount the persistent Railway volume at **`/data`** and keep the database inside it. Retain the existing volume and database when updating the bot.

Example variables matching the current deployment configuration:

```dotenv
DISCORD_TOKEN=<your bot token, set privately in Railway>
API_URL=https://chronicles.wfitapp.xyz/api/raid/leaderboard?limit=50
POLL_SECONDS=60
DAILY_TIMEZONE=Europe/Berlin
DB_FILE=/data/war_bunker.sqlite3
```

`POLL_SECONDS` supports 20–3600 seconds. The API URL must use HTTPS on `chronicles.wfitapp.xyz`. Daily timezone names use the IANA format, such as `Europe/Berlin` or `UTC`.

**Keep the existing timezone once v2 data has been recorded.** The bot rejects a different timezone for an existing database to protect historical date boundaries.

Store tokens only in Railway variables or your local environment. The bot does not automatically load a `.env` file.

After deployment, check for `RaidOps v2 ready` and recurring successful `Snapshot` logs. Use `/status` to check freshness and the delivery queue. Commands are registered globally; startup removes old server-specific copies to prevent duplicates. Refresh Discord if its command picker is stale.

## Persistence and delivery

The volume stores tracker configuration, raid counter baselines, daily observations, queued reports, and personal ping subscriptions. Automatic reports are queued persistently and retried after delivery failures. Disabling or removing a tracker cancels its pending reports while observation continues.

Delivery is **at least once**: a crash after Discord accepts a message but before the database acknowledges it can produce duplicate reports or pings. Large subscriber lists use extra mention messages after the single attack report.

Back up the database before major upgrades. A plain copy of a running SQLite main file can omit pending WAL transactions; use a SQLite-aware backup or a suitable volume backup.

For a live check, have an API-visible participant attack, confirm the correct channel receives the increase, and compare `/daily` with `/activity`. After a redeploy, confirm settings and history remain available. Monitor queue growth, poll errors, disk usage, and hosting availability.
