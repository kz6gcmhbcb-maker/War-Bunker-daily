import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

BOT_NAME = "WChronicles RaidOps"
API_URL = os.getenv("API_URL", "https://chronicles.wfitapp.xyz/api/raid/leaderboard?limit=60")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
TIMEZONE = os.getenv("DAILY_TIMEZONE", "UTC")
DB_FILE = Path(os.getenv("DB_FILE", "/data/war_bunker.sqlite3"))

try:
    LOCAL_TZ = ZoneInfo(TIMEZONE)
except Exception:
    LOCAL_TZ = timezone.utc
    TIMEZONE = "UTC"

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
monitor_started = False
configs = {}

# One Discord server can have up to six independent faction trackers.
MAX_TRACKERS_PER_GUILD = 6


def db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS players_daily(
            day TEXT NOT NULL, player_id TEXT NOT NULL, player_name TEXT NOT NULL,
            faction TEXT NOT NULL, start_attacks INTEGER NOT NULL DEFAULT 0,
            last_attacks INTEGER NOT NULL DEFAULT 0, attacks_today INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT, last_seen TEXT, PRIMARY KEY(day, player_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS attack_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, detected_at TEXT NOT NULL,
            player_id TEXT NOT NULL, player_name TEXT NOT NULL, faction TEXT NOT NULL,
            delta INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_meta(
            day TEXT PRIMARY KEY, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tracker_config(
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, faction TEXT NOT NULL,
            alerts INTEGER NOT NULL DEFAULT 1,
            UNIQUE(guild_id, faction))""")
        # Migrate the old v1.x one-tracker-per-guild table.
        old = c.execute("""SELECT 1 FROM sqlite_master
                           WHERE type='table' AND name='guild_config'""").fetchone()
        if old:
            c.execute("""INSERT OR IGNORE INTO tracker_config
                        (guild_id, channel_id, faction, alerts)
                        SELECT guild_id, channel_id, faction, alerts FROM guild_config""")
        c.commit()


def load_configs():
    global configs
    configs = {}
    with db() as c:
        rows = c.execute("""SELECT guild_id, channel_id, faction, alerts
                            FROM tracker_config ORDER BY guild_id, faction COLLATE NOCASE""").fetchall()
    for r in rows:
        gid = str(r["guild_id"])
        key = r["faction"].strip().lower()
        configs.setdefault(gid, {})[key] = {
            "channel_id": str(r["channel_id"]),
            "faction": r["faction"].strip(),
            "alerts": bool(r["alerts"])
        }


def trackers(guild_id):
    return list(configs.get(str(guild_id), {}).values())


def save_tracker(guild_id, channel_id, faction):
    faction = faction.strip()
    if not faction:
        raise ValueError("Faction cannot be empty")
    with db() as c:
        exists = c.execute("""SELECT 1 FROM tracker_config
                              WHERE guild_id=? AND lower(faction)=lower(?)""",
                           (str(guild_id), faction)).fetchone()
        count = c.execute("""SELECT COUNT(*) FROM tracker_config WHERE guild_id=?""",
                          (str(guild_id),)).fetchone()[0]
        if not exists and count >= MAX_TRACKERS_PER_GUILD:
            raise ValueError(f"Maximum {MAX_TRACKERS_PER_GUILD} trackers per server")
        c.execute("""INSERT INTO tracker_config(guild_id, channel_id, faction, alerts)
                     VALUES(?,?,?,1)
                     ON CONFLICT(guild_id,faction) DO UPDATE SET
                     channel_id=excluded.channel_id, faction=excluded.faction""",
                  (str(guild_id), str(channel_id), faction))
        c.commit()
    load_configs()


def remove_tracker(guild_id, faction):
    with db() as c:
        cur = c.execute("""DELETE FROM tracker_config
                           WHERE guild_id=? AND lower(faction)=lower(?)""",
                        (str(guild_id), faction.strip()))
        c.commit()
    load_configs()
    return cur.rowcount


def set_alerts(guild_id, enabled, faction=None):
    with db() as c:
        if faction:
            cur = c.execute("""UPDATE tracker_config SET alerts=?
                               WHERE guild_id=? AND lower(faction)=lower(?)""",
                            (int(enabled), str(guild_id), faction.strip()))
        else:
            cur = c.execute("""UPDATE tracker_config SET alerts=? WHERE guild_id=?""",
                            (int(enabled), str(guild_id)))
        c.commit()
    load_configs()
    return cur.rowcount


def selected_faction(guild_id, faction=None):
    ts = trackers(guild_id)
    if faction:
        for t in ts:
            if t["faction"].lower() == faction.strip().lower():
                return t["faction"]
        return None
    if len(ts) == 1:
        return ts[0]["faction"]
    return None


async def fetch_api():
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": f"{BOT_NAME}/1.5.1"}) as s:
        async with s.get(API_URL) as r:
            r.raise_for_status()
            return await r.json()


def entries_from(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("entries", "players", "data", "leaderboard", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def norm(x):
    if not isinstance(x, dict):
        return None
    pid = x.get("id") or x.get("player_id") or x.get("uuid") or x.get("user_id")
    name = x.get("name") or x.get("player_name") or x.get("username") or x.get("player")
    faction = x.get("faction") or x.get("guild") or x.get("team")
    attacks = x.get("attacks", x.get("attack_count", 0))
    if pid is None or name is None or faction is None:
        return None
    try:
        attacks = int(attacks or 0)
    except (TypeError, ValueError):
        attacks = 0
    return {"id": str(pid), "name": str(name), "faction": str(faction), "attacks": attacks}


def record(entries):
    day = datetime.now(timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    seen = datetime.now(timezone.utc).isoformat()
    events = []
    with db() as c:
        c.execute("INSERT OR IGNORE INTO daily_meta(day,created_at) VALUES(?,?)", (day, seen))
        for raw in entries:
            e = norm(raw)
            if not e:
                continue
            row = c.execute("SELECT * FROM players_daily WHERE day=? AND player_id=?",
                            (day, e["id"])).fetchone()
            if row is None:
                c.execute("""INSERT INTO players_daily
                    (day,player_id,player_name,faction,start_attacks,last_attacks,
                     attacks_today,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?,0,?,?)""",
                    (day,e["id"],e["name"],e["faction"],e["attacks"],e["attacks"],seen,seen))
                continue
            delta = max(0, e["attacks"] - int(row["last_attacks"]))
            c.execute("""UPDATE players_daily SET player_name=?,faction=?,last_attacks=?,
                         attacks_today=attacks_today+?,last_seen=?
                         WHERE day=? AND player_id=?""",
                      (e["name"],e["faction"],e["attacks"],delta,seen,day,e["id"]))
            if delta:
                c.execute("""INSERT INTO attack_events
                    (day,detected_at,player_id,player_name,faction,delta)
                    VALUES(?,?,?,?,?,?)""",
                    (day,seen,e["id"],e["name"],e["faction"],delta))
                events.append({**e,"delta":delta,"detected_at":seen})
        c.commit()
    return events


def fmt_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(LOCAL_TZ).strftime("%H:%M:%S")
    except Exception:
        return "—"


def embed_events(faction, events):
    e = discord.Embed(title=f"⚔️ {faction} — Raid activity",
                      description=f"{len(events)} new attack detection(s).",
                      timestamp=datetime.now(timezone.utc))
    for x in events[:20]:
        e.add_field(name=x["name"],
                    value=f"+{x['delta']} attack(s) • detected {fmt_time(x['detected_at'])} {TIMEZONE}",
                    inline=False)
    if len(events) > 20:
        e.set_footer(text=f"+{len(events)-20} more")
    return e


@tree.command(name="setup", description="Set one faction to one Discord channel")
@app_commands.describe(channel="Channel for this faction", faction="Faction name")
async def setup(interaction: discord.Interaction, channel: discord.TextChannel, faction: str):
    try:
        existed = any(t["faction"].lower() == faction.strip().lower() for t in trackers(interaction.guild_id))
        save_tracker(interaction.guild_id, channel.id, faction)
        count = len(trackers(interaction.guild_id))
        action = "updated" if existed else "added"
        await interaction.response.send_message(
            f"✅ **{faction.strip()}** {action} → {channel.mention}\n"
            f"Active trackers: **{count}/{MAX_TRACKERS_PER_GUILD}**. Другите trackers не се пипат."
        )
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


@tree.command(name="status", description="List all faction/channel trackers")
async def status(interaction: discord.Interaction):
    ts = trackers(interaction.guild_id)
    if not ts:
        await interaction.response.send_message("Няма trackers. Започни с `/setup`.")
        return
    lines = []
    for i, t in enumerate(ts, 1):
        ch = interaction.guild.get_channel(int(t["channel_id"]))
        mention = ch.mention if ch else f"<#{t['channel_id']}>"
        lines.append(f"`{i}` **{t['faction']}** → {mention} • alerts `{'ON' if t['alerts'] else 'OFF'}`")
    await interaction.response.send_message(
        f"📡 **RaidOps — {len(ts)}/{MAX_TRACKERS_PER_GUILD} trackers**\n" + "\n".join(lines)
    )


@tree.command(name="remove_tracker", description="Remove a faction tracker")
@app_commands.describe(faction="Faction to remove")
async def remove(interaction: discord.Interaction, faction: str):
    if remove_tracker(interaction.guild_id, faction):
        await interaction.response.send_message(f"🗑️ Removed **{faction.strip()}** tracker.")
    else:
        await interaction.response.send_message(f"Няма tracker за **{faction.strip()}**.", ephemeral=True)


@tree.command(name="enable", description="Enable alerts for one faction or all")
@app_commands.describe(faction="Optional faction")
async def enable(interaction: discord.Interaction, faction: str | None = None):
    n = set_alerts(interaction.guild_id, True, faction)
    await interaction.response.send_message(
        f"🔔 Alerts ON: **{faction.strip()}**." if faction else f"🔔 Alerts ON for **{n}** tracker(s)."
    )


@tree.command(name="disable", description="Disable alerts for one faction or all")
@app_commands.describe(faction="Optional faction")
async def disable(interaction: discord.Interaction, faction: str | None = None):
    n = set_alerts(interaction.guild_id, False, faction)
    await interaction.response.send_message(
        f"🔕 Alerts OFF: **{faction.strip()}**." if faction else f"🔕 Alerts OFF for **{n}** tracker(s)."
    )


@tree.command(name="daily", description="Show today's or a selected day's attacks")
@app_commands.describe(faction="Faction", day="YYYY-MM-DD")
async def daily(interaction: discord.Interaction, faction: str | None = None, day: str | None = None):
    fac = selected_faction(interaction.guild_id, faction)
    if faction and not fac:
        await interaction.response.send_message(f"Няма tracker за **{faction}**.", ephemeral=True)
        return
    if not fac:
        ts = trackers(interaction.guild_id)
        if len(ts) > 1:
            await interaction.response.send_message(
                "Има 2+ trackers. Посочи faction в `/daily`.", ephemeral=True)
            return
        if not ts:
            await interaction.response.send_message("Няма настроен tracker.", ephemeral=True)
            return
    day = day or datetime.now(timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute("""SELECT player_name,attacks_today FROM players_daily
                           WHERE day=? AND lower(faction)=lower(?)
                           ORDER BY attacks_today DESC, player_name COLLATE NOCASE""",
                        (day,fac)).fetchall()
    if not rows:
        await interaction.response.send_message(f"Няма записи за **{fac}** на {day}.")
        return
    text = "\n".join(f"`{i:02}` **{r['player_name']}** — `{r['attacks_today']}` attack(s)"
                     for i,r in enumerate(rows[:50],1))
    e = discord.Embed(title=f"📅 Daily — {fac}", description=text)
    e.set_footer(text=f"{day} • {TIMEZONE}")
    await interaction.response.send_message(embed=e)


@tree.command(name="dailyhistory", description="Show daily history for YYYY-MM-DD")
async def dailyhistory(interaction: discord.Interaction, day: str):
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        await interaction.response.send_message("Използвай YYYY-MM-DD.", ephemeral=True)
        return
    with db() as c:
        rows = c.execute("""SELECT faction,player_name,attacks_today FROM players_daily
                            WHERE day=? ORDER BY faction,attacks_today DESC,player_name""",
                         (day,)).fetchall()
    if not rows:
        await interaction.response.send_message(f"Няма история за {day}.")
        return
    grouped = {}
    for r in rows:
        grouped.setdefault(r["faction"], []).append(r)
    out=[]
    for fac, items in grouped.items():
        out.append(f"**{fac}**")
        out.extend(f"• {x['player_name']}: {x['attacks_today']}" for x in items[:15])
    await interaction.response.send_message(f"📚 **History {day}**\n" + "\n".join(out)[:3900])


@tree.command(name="activity", description="Show latest detected attacks")
async def activity(interaction: discord.Interaction, faction: str | None = None):
    with db() as c:
        if faction:
            rows=c.execute("""SELECT * FROM attack_events WHERE day=? AND lower(faction)=lower(?)
                             ORDER BY id DESC LIMIT 20""",
                           (datetime.now(timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d"),faction)).fetchall()
        else:
            rows=c.execute("""SELECT * FROM attack_events WHERE day=? ORDER BY id DESC LIMIT 20""",
                           (datetime.now(timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d"),)).fetchall()
    if not rows:
        await interaction.response.send_message("Няма засечена activity днес.")
        return
    await interaction.response.send_message("\n".join(
        f"• **{r['player_name']}** ({r['faction']}) +{r['delta']} at {fmt_time(r['detected_at'])}"
        for r in rows
    ))


@tree.command(name="update", description="Force an API update now")
async def update(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        events=record(entries_from(await fetch_api()))
        await interaction.followup.send(f"✅ Updated. New attack events: **{len(events)}**.")
    except Exception as exc:
        await interaction.followup.send(f"❌ API error: `{type(exc).__name__}: {exc}`")


@tree.command(name="top5", description="Current top 5")
async def top5(interaction: discord.Interaction, faction: str | None = None):
    await current_top(interaction, 5, faction)


@tree.command(name="top10", description="Current top 10")
async def top10(interaction: discord.Interaction, faction: str | None = None):
    await current_top(interaction, 10, faction)


async def current_top(interaction, n, faction):
    try:
        xs=[norm(x) for x in entries_from(await fetch_api())]
        xs=[x for x in xs if x and (not faction or x["faction"].lower()==faction.lower())]
        xs.sort(key=lambda x:x["attacks"], reverse=True)
        lines=[f"`{i:02}` **{x['name']}** — {x['attacks']} ({x['faction']})"
                for i,x in enumerate(xs[:n],1)]
        await interaction.response.send_message(f"🏆 **Top {n}**\n" + ("\n".join(lines) or "No data."))
    except Exception as exc:
        await interaction.response.send_message(f"❌ API error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="topfactions", description="Current faction totals")
async def topfactions(interaction: discord.Interaction):
    try:
        totals={}
        for x in (norm(v) for v in entries_from(await fetch_api())):
            if x: totals[x["faction"]]=totals.get(x["faction"],0)+x["attacks"]
        lines=[f"`{i}` **{k}** — {v}" for i,(k,v) in enumerate(sorted(totals.items(),key=lambda z:z[1],reverse=True)[:10],1)]
        await interaction.response.send_message("🏰 **Top factions**\n"+("\n".join(lines) or "No data."))
    except Exception as exc:
        await interaction.response.send_message(f"❌ API error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="stats", description="Stats for a faction")
async def stats(interaction: discord.Interaction, faction: str):
    try:
        xs=[norm(v) for v in entries_from(await fetch_api())]
        xs=[x for x in xs if x and x["faction"].lower()==faction.lower()]
        if not xs:
            await interaction.response.send_message(f"No data for **{faction}**.")
            return
        total=sum(x["attacks"] for x in xs)
        await interaction.response.send_message(f"📊 **{faction}** — {len(xs)} players • {total} attacks • avg `{total/len(xs):.1f}`")
    except Exception as exc:
        await interaction.response.send_message(f"❌ API error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="gap", description="Attack gap inside a faction")
async def gap(interaction: discord.Interaction, faction: str):
    try:
        xs=[norm(v) for v in entries_from(await fetch_api())]
        xs=[x for x in xs if x and x["faction"].lower()==faction.lower()]
        if not xs:
            await interaction.response.send_message(f"No data for **{faction}**.")
            return
        xs.sort(key=lambda x:x["attacks"],reverse=True)
        await interaction.response.send_message(
            f"📐 **{faction}** gap: `{xs[0]['attacks']-xs[-1]['attacks']}` "
            f"({xs[0]['name']} {xs[0]['attacks']} → {xs[-1]['name']} {xs[-1]['attacks']})")
    except Exception as exc:
        await interaction.response.send_message(f"❌ API error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="intel", description="Bot configuration")
async def intel(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🤖 **{BOT_NAME} v1.5.1**\nAPI limit `60` • Poll `{POLL_SECONDS}s` • Timezone `{TIMEZONE}`\n"
        f"Trackers `{len(trackers(interaction.guild_id))}/{MAX_TRACKERS_PER_GUILD}` • DB `{DB_FILE}`")


@tasks.loop(seconds=POLL_SECONDS)
async def monitor():
    try:
        events=record(entries_from(await fetch_api()))
        if not events:
            return
        # Crucial: every configured faction gets its own independent channel.
        for gid, faction_map in list(configs.items()):
            guild=client.get_guild(int(gid))
            if not guild:
                continue
            for t in faction_map.values():
                if not t["alerts"]:
                    continue
                relevant=[e for e in events if e["faction"].strip().lower()==t["faction"].strip().lower()]
                if not relevant:
                    continue
                channel=guild.get_channel(int(t["channel_id"]))
                if channel:
                    try:
                        await channel.send(embed=embed_events(t["faction"], relevant))
                    except Exception as exc:
                        print(f"[monitor] send failed {guild.id}/{t['faction']}: {exc}")
    except Exception as exc:
        print(f"[monitor] {type(exc).__name__}: {exc}")


@monitor.before_loop
async def before_monitor():
    await client.wait_until_ready()


async def sync_commands():
    for guild in client.guilds:
        try:
            tree.copy_global_to(guild=guild)
            synced=await tree.sync(guild=guild)
            print(f"[sync] {guild.name}: {len(synced)} commands")
        except Exception as exc:
            print(f"[sync] guild {guild.id}: {exc}")
    try:
        await tree.sync()
        print("[sync] global commands synced")
    except Exception as exc:
        print(f"[sync] global: {exc}")


@client.event
async def on_ready():
    global monitor_started
    init_db()
    load_configs()
    print(f"[ready] {BOT_NAME} as {client.user}")
    print(f"[ready] DB={DB_FILE} TZ={TIMEZONE} poll={POLL_SECONDS}s trackers={sum(len(v) for v in configs.values())}")
    if not monitor_started:
        await sync_commands()
        monitor.start()
        monitor_started=True


client.run(TOKEN)
