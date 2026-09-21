
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

VERSION = "1.5.0"
BOT_NAME = "WChronicles RaidOps"
API_URL = os.getenv(
    "API_URL",
    "https://chronicles.wfitapp.xyz/api/raid/leaderboard?limit=60",
)
TOKEN = os.getenv("DISCORD_TOKEN")
POLL_SECONDS = max(20, int(os.getenv("POLL_SECONDS", "60")))
TIMEZONE = "UTC"
DB_FILE = Path(os.getenv("DB_FILE", "/data/war_bunker.sqlite3"))

intents = discord.Intents.default()
intents.guilds = True
client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree
configs = {}


def now():
    return datetime.now(timezone.utc)


def iso_now():
    return now().isoformat(timespec="seconds").replace("+00:00", "Z")


def day_key():
    return now().date().isoformat()


def db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS players_daily (
            day TEXT NOT NULL,
            uid TEXT NOT NULL,
            name TEXT NOT NULL,
            faction TEXT NOT NULL,
            start_attacks INTEGER NOT NULL DEFAULT 0,
            last_attacks INTEGER NOT NULL DEFAULT 0,
            attacks_today INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT NOT NULL,
            PRIMARY KEY(day, uid)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_meta (
            day TEXT PRIMARY KEY,
            started_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attack_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            uid TEXT NOT NULL,
            name TEXT NOT NULL,
            faction TEXT NOT NULL,
            delta INTEGER NOT NULL,
            detected_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            faction TEXT NOT NULL,
            alerts INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def iv(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def fmt(value):
    return f"{iv(value):,}"


def pname(p):
    return str(p.get("displayName") or p.get("username") or p.get("name") or p.get("uid") or "Unknown")


def puid(p):
    return str(p.get("uid") or p.get("id") or pname(p))


def pfaction(p):
    return str(p.get("factionName") or p.get("faction") or "Unknown")


def pattacks(p):
    return iv(p.get("attacks"))


def ppoints(p):
    return iv(p.get("factionPoints", p.get("points")))


def pdamage(p):
    return iv(p.get("totalDamage", p.get("damage")))


async def fetch():
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://chronicles.wfitapp.xyz/",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(API_URL, headers=headers) as response:
            response.raise_for_status()
            data = await response.json()
            if not isinstance(data, dict):
                raise ValueError("API returned an unexpected response.")
            return data


def factions(data):
    return sorted(data.get("factions", []), key=lambda f: iv(f.get("rank")) or 999999)


def get_faction(data, name):
    q = str(name or "").strip().lower()
    return next(
        (f for f in data.get("factions", [])
         if str(f.get("factionName", "")).strip().lower() == q),
        None,
    )


def players(data, faction=None):
    rows = list(data.get("entries", []))
    if faction:
        q = faction.strip().lower()
        rows = [p for p in rows if pfaction(p).strip().lower() == q]
    return sorted(rows, key=lambda p: (ppoints(p), pdamage(p), pattacks(p)), reverse=True)


def load_configs():
    global configs
    with db() as conn:
        rows = conn.execute("SELECT * FROM guild_config").fetchall()
    configs = {
        row["guild_id"]: {
            "channel_id": row["channel_id"],
            "faction": row["faction"],
            "alerts": bool(row["alerts"]),
        }
        for row in rows
    }


def save_config(guild_id, channel_id, faction):
    with db() as conn:
        conn.execute("""
            INSERT INTO guild_config(guild_id, channel_id, faction, alerts)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                faction=excluded.faction
        """, (str(guild_id), str(channel_id), faction))
        conn.commit()
    configs[str(guild_id)] = {"channel_id": str(channel_id), "faction": faction, "alerts": True}


def set_alerts(guild_id, enabled):
    with db() as conn:
        conn.execute("UPDATE guild_config SET alerts=? WHERE guild_id=?", (int(enabled), str(guild_id)))
        conn.commit()
    if str(guild_id) in configs:
        configs[str(guild_id)]["alerts"] = enabled


def configured_faction(guild_id, faction=None):
    return faction or configs.get(str(guild_id), {}).get("faction")


def ensure_daily_meta(conn, day):
    row = conn.execute("SELECT started_at FROM daily_meta WHERE day=?", (day,)).fetchone()
    if row:
        return row["started_at"]
    stamp = iso_now()
    conn.execute("INSERT INTO daily_meta(day, started_at) VALUES (?, ?)", (day, stamp))
    return stamp


def record_daily(data):
    today = day_key()
    stamp = iso_now()
    events = []

    with db() as conn:
        ensure_daily_meta(conn, today)

        for p in data.get("entries", []):
            uid, name, faction, current = puid(p), pname(p), pfaction(p), pattacks(p)
            row = conn.execute(
                "SELECT * FROM players_daily WHERE day=? AND uid=?",
                (today, uid),
            ).fetchone()

            if row is None:
                conn.execute("""
                    INSERT INTO players_daily
                    (day, uid, name, faction, start_attacks, last_attacks, attacks_today, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """, (today, uid, name, faction, current, current, stamp))
                continue

            previous = iv(row["last_attacks"])
            delta = max(0, current - previous)

            if delta:
                event = {
                    "uid": uid, "name": name, "faction": faction,
                    "delta": delta, "detected_at": stamp,
                }
                events.append(event)
                conn.execute("""
                    INSERT INTO attack_events(day, uid, name, faction, delta, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (today, uid, name, faction, delta, stamp))

            conn.execute("""
                UPDATE players_daily
                SET name=?, faction=?, last_attacks=?, attacks_today=attacks_today+?, last_seen=?
                WHERE day=? AND uid=?
            """, (name, faction, current, delta, stamp, today, uid))

        conn.commit()

    return events


def parse_date(value):
    if not value:
        return day_key()
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("Date must be YYYY-MM-DD.") from exc


def time_only(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return value


def daily_rows(faction, target_day):
    with db() as conn:
        meta = conn.execute(
            "SELECT started_at FROM daily_meta WHERE day=?", (target_day,)
        ).fetchone()
        rows = conn.execute("""
            SELECT p.*,
                   (SELECT MAX(e.detected_at)
                    FROM attack_events e
                    WHERE e.day=p.day AND e.uid=p.uid) AS last_attack_at
            FROM players_daily p
            WHERE p.day=? AND lower(p.faction)=lower(?)
            ORDER BY attacks_today DESC, name COLLATE NOCASE
        """, (target_day, faction)).fetchall()
    return (meta["started_at"] if meta else None), rows


def daily_embed(faction, view="all", target_day=None):
    target_day = target_day or day_key()
    started, rows = daily_rows(faction, target_day)
    attacked = [r for r in rows if iv(r["attacks_today"]) > 0]
    missing = [r for r in rows if iv(r["attacks_today"]) == 0]

    shown = rows if view == "all" else attacked if view == "attacked" else missing
    title = {
        "all": "DAILY ATTACK CHECK",
        "attacked": "ATTACKED",
        "missing": "NOT ATTACKED",
    }.get(view, "DAILY ATTACK CHECK")

    e = discord.Embed(title=f"⚡ {faction.upper()} — {title}", color=0x42F5C5)
    if not rows:
        e.description = f"No roster captured for **{target_day}**."
        e.set_footer(text=f"{BOT_NAME} v{VERSION} • UTC")
        return e

    e.description = (
        f"Day: **{target_day}** • Roster: **{len(rows)}** • "
        f"Attacked: **{len(attacked)}** • Missing: **{len(missing)}**"
    )

    lines = []
    for i, row in enumerate(shown, 1):
        attacks = iv(row["attacks_today"])
        if attacks:
            lines.append(
                f"{i}. **{row['name']}** — **{attacks}** attack(s) • 🕒 **{time_only(row['last_attack_at'])} UTC**"
            )
        else:
            lines.append(f"{i}. **{row['name']}** — **0** attacks")

    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) + 1 > 1000:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        e.add_field(name="PLAYERS" if i == 0 else "CONTINUED", value=chunk, inline=False)

    coverage = f"Tracker started {time_only(started)} UTC." if started else "Tracker start time unavailable."
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • {coverage} • attack times = detection times")
    return e


def activity_embed(faction, events):
    lines = [
        f"• **{x['name']}** +{x['delta']} attack(s) • 🕒 **{time_only(x['detected_at'])} UTC**"
        for x in events
    ]
    e = discord.Embed(title="⚡ RAID ACTIVITY", description="\n".join(lines), color=0x42F5C5)
    e.add_field(name="TRACKED FACTION", value=f"**{faction}**", inline=False)
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • UTC • detection timestamp")
    return e


def factions_embed(data):
    e = discord.Embed(title="⚡ FACTION LEADERBOARD", color=0x42F5C5)
    for i, f in enumerate(factions(data), 1):
        rank = iv(f.get("rank")) or i
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"#{rank}"
        e.add_field(
            name=f"{medal} {f.get('factionName', 'Unknown')}",
            value=(
                f"Points: **{fmt(f.get('points'))}** • Damage: **{fmt(f.get('damage'))}**\n"
                f"Attacks: **{fmt(f.get('attacks'))}** • Players: **{fmt(f.get('players'))}** • Wins: **{fmt(f.get('wins'))}**"
            ),
            inline=False,
        )
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • LIVE DATA")
    return e


def top_embed(data, count=5, faction=None):
    rows = players(data, faction)[:count]
    title = f"⚡ TOP {count} PLAYERS" + (f" — {faction.upper()}" if faction else "")
    e = discord.Embed(title=title, color=0x42F5C5)
    for i, p in enumerate(rows, 1):
        e.add_field(
            name=f"{i}. {pname(p)}",
            value=f"Points: **{fmt(ppoints(p))}** • Damage: **{fmt(pdamage(p))}** • Attacks: **{fmt(pattacks(p))}**",
            inline=False,
        )
    if not rows:
        e.description = "No players found."
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • LIVE DATA")
    return e


def raid_embed(data):
    fs = data.get("factions", [])
    e = discord.Embed(
        title=f"⚡ {str(data.get('name') or 'RAID').upper()}",
        description=f"Status: **{data.get('status', 'Unknown')}**",
        color=0x42F5C5,
    )
    for label, value in [
        ("FACTIONS", len(fs)),
        ("TOTAL POINTS", sum(iv(f.get("points")) for f in fs)),
        ("TOTAL DAMAGE", sum(iv(f.get("damage")) for f in fs)),
        ("TOTAL ATTACKS", sum(iv(f.get("attacks")) for f in fs)),
    ]:
        e.add_field(name=label, value=fmt(value), inline=True)
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • LIVE DATA")
    return e


def stats_embed(data, faction):
    f = get_faction(data, faction)
    e = discord.Embed(title=f"⚡ {faction.upper()} — STATS", color=0x42F5C5)
    if not f:
        e.description = "Faction not found."
        return e
    e.description = f"Rank **#{iv(f.get('rank'))}**"
    for label, key in [
        ("POINTS", "points"), ("DAMAGE", "damage"),
        ("ATTACKS", "attacks"), ("PLAYERS", "players"), ("WINS", "wins"),
    ]:
        e.add_field(name=label, value=fmt(f.get(key)), inline=True)
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • LIVE DATA")
    return e


def gap_embed(data, faction):
    fs = factions(data)
    f = get_faction(data, faction)
    e = discord.Embed(title=f"⚡ {faction.upper()} — GAP", color=0x42F5C5)
    if not f:
        e.description = "Faction not found."
        return e
    idx = next(
        (i for i, x in enumerate(fs)
         if str(x.get("factionName", "")).lower() == str(f.get("factionName", "")).lower()),
        None,
    )
    if idx == 0:
        e.description = "Rank **#1** — no faction above."
    elif idx is not None:
        above = fs[idx - 1]
        gap = max(0, iv(above.get("points")) - iv(f.get("points")))
        e.description = (
            f"Current: **#{iv(f.get('rank'))} {f.get('factionName')}**\n"
            f"Above: **{above.get('factionName')}**\n"
            f"Points gap: **{fmt(gap)}**"
        )
    else:
        e.description = "Rank data unavailable."
    return e


def intel_embed(data, faction):
    f = get_faction(data, faction)
    e = discord.Embed(title=f"⚡ {faction.upper()} — INTEL", color=0x42F5C5)
    if not f:
        e.description = "Faction not found."
        return e
    e.description = f"Rank **#{iv(f.get('rank'))}**"
    fs = factions(data)
    idx = next(
        (i for i, x in enumerate(fs)
         if str(x.get("factionName", "")).lower() == str(f.get("factionName", "")).lower()),
        None,
    )
    if idx is not None and idx > 0:
        above = fs[idx - 1]
        e.add_field(
            name="NEXT TARGET",
            value=f"**{above.get('factionName')}**\nPoints gap: **{fmt(max(0, iv(above.get('points')) - iv(f.get('points'))))}**",
            inline=False,
        )
    rows = players(data, faction)[:3]
    e.add_field(
        name="TOP 3",
        value="\n".join(
            f"{i}. **{pname(p)}** — {fmt(ppoints(p))} points"
            for i, p in enumerate(rows, 1)
        ) or "No player data.",
        inline=False,
    )
    return e


async def faction_autocomplete(interaction, current):
    try:
        names = [str(f.get("factionName", "")).strip() for f in (await fetch()).get("factions", [])]
    except Exception:
        names = []
    q = current.lower().strip()
    return [
        app_commands.Choice(name=n, value=n)
        for n in names if n and (not q or q in n.lower())
    ][:25]


@tree.command(name="setup", description="Configure the automatic channel and faction.")
@app_commands.describe(channel="Channel for automatic raid alerts.", faction="Faction to track.")
@app_commands.autocomplete(faction=faction_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(interaction, channel: discord.TextChannel, faction: str):
    data = await fetch()
    f = get_faction(data, faction)
    if not f:
        await interaction.response.send_message("Faction not found. Choose from autocomplete.", ephemeral=True)
        return
    name = str(f.get("factionName"))
    save_config(interaction.guild_id, channel.id, name)
    await interaction.response.send_message(
        f"⚡ **{BOT_NAME} v{VERSION} configured**\n"
        f"Channel: {channel.mention}\nFaction: **{name}**\n"
        f"Automatic alerts: **ON**\nDaily tracking: **ON**\nTimezone: **UTC**\nAPI limit: **60**",
        ephemeral=True,
    )


@tree.command(name="status", description="Show bot configuration and storage status.")
async def status(interaction):
    c = configs.get(str(interaction.guild_id))
    if not c:
        await interaction.response.send_message("Not configured. Use `/setup`.", ephemeral=True)
        return
    ch = interaction.guild.get_channel(iv(c["channel_id"]))
    try:
        with db() as conn:
            db_ok = conn.execute("SELECT 1").fetchone() is not None
    except Exception:
        db_ok = False
    await interaction.response.send_message(
        f"**{BOT_NAME} v{VERSION}**\n"
        f"Channel: {ch.mention if ch else 'not found'}\n"
        f"Faction: **{c['faction']}**\n"
        f"Alerts: **{'ON' if c['alerts'] else 'OFF'}**\n"
        f"Timezone: **UTC**\nAPI limit: **60**\n"
        f"Database: **{'OK' if db_ok else 'ERROR'}**\n"
        f"DB path: `{DB_FILE}`",
        ephemeral=True,
    )


@tree.command(name="daily", description="Show daily attacks: all, attacked, or missing.")
@app_commands.describe(
    faction="Optional; defaults to the server faction.",
    view="Choose all, attacked, or missing.",
    date="Optional UTC date in YYYY-MM-DD.",
)
@app_commands.autocomplete(faction=faction_autocomplete)
@app_commands.choices(view=[
    app_commands.Choice(name="all", value="all"),
    app_commands.Choice(name="attacked", value="attacked"),
    app_commands.Choice(name="missing", value="missing"),
])
async def daily(interaction, faction: Optional[str] = None, view: str = "all", date: Optional[str] = None):
    faction = configured_faction(interaction.guild_id, faction)
    if not faction:
        await interaction.response.send_message("Choose a faction or use `/setup`.", ephemeral=True)
        return
    try:
        target = parse_date(date)
    except ValueError as ex:
        await interaction.response.send_message(str(ex), ephemeral=True)
        return
    await interaction.response.defer()
    try:
        if target == day_key():
            record_daily(await fetch())
        await interaction.followup.send(embed=daily_embed(faction, view, target))
    except Exception as ex:
        await interaction.followup.send(f"⚠️ Daily tracker error: `{type(ex).__name__}`")


@tree.command(name="dailyhistory", description="Show recently recorded daily history.")
@app_commands.describe(days="Number of days to show, 1-30.")
async def dailyhistory(interaction, days: Optional[int] = 7):
    days = max(1, min(days or 7, 30))
    await interaction.response.defer()
    with db() as conn:
        rows = conn.execute("""
            SELECT d.day, d.started_at,
                   COUNT(p.uid) roster,
                   COALESCE(SUM(CASE WHEN p.attacks_today > 0 THEN 1 ELSE 0 END), 0) attacked
            FROM daily_meta d
            LEFT JOIN players_daily p ON p.day=d.day
            GROUP BY d.day, d.started_at
            ORDER BY d.day DESC
            LIMIT ?
        """, (days,)).fetchall()

    e = discord.Embed(title="⚡ DAILY HISTORY", color=0x42F5C5)
    e.description = "\n".join(
        f"**{r['day']}** — {iv(r['attacked'])}/{iv(r['roster'])} attacked • started {time_only(r['started_at'])} UTC"
        for r in rows
    ) if rows else "No history recorded yet."
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • persistent SQLite history")
    await interaction.followup.send(embed=e)


@tree.command(name="activity", description="Show today's recorded attack events.")
@app_commands.describe(faction="Optional; defaults to server faction.", limit="Number of events, 1-20.")
@app_commands.autocomplete(faction=faction_autocomplete)
async def activity(interaction, faction: Optional[str] = None, limit: Optional[int] = 10):
    faction = configured_faction(interaction.guild_id, faction)
    if not faction:
        await interaction.response.send_message("Choose a faction or use `/setup`.", ephemeral=True)
        return
    limit = max(1, min(limit or 10, 20))
    await interaction.response.defer()
    with db() as conn:
        rows = conn.execute("""
            SELECT name, delta, detected_at
            FROM attack_events
            WHERE day=? AND lower(faction)=lower(?)
            ORDER BY id DESC
            LIMIT ?
        """, (day_key(), faction, limit)).fetchall()
    e = discord.Embed(title=f"⚡ {faction.upper()} — RECENT ACTIVITY", color=0x42F5C5)
    e.description = "\n".join(
        f"**{r['name']}** +{iv(r['delta'])} attack(s) • 🕒 {time_only(r['detected_at'])} UTC"
        for r in rows
    ) if rows else "No attacks recorded today."
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • detection timestamps")
    await interaction.followup.send(embed=e)


@tree.command(name="update", description="Post the live faction leaderboard to the configured channel.")
@app_commands.checks.has_permissions(manage_guild=True)
async def update_cmd(interaction):
    c = configs.get(str(interaction.guild_id))
    if not c:
        await interaction.response.send_message("Use `/setup` first.", ephemeral=True)
        return
    ch = interaction.guild.get_channel(iv(c["channel_id"]))
    if not ch:
        await interaction.response.send_message("Configured channel not found.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await ch.send(embed=factions_embed(await fetch()))
    await interaction.followup.send("⚡ Live leaderboard posted.", ephemeral=True)


@tree.command(name="top5", description="Show top 5 players.")
@app_commands.describe(faction="Optional faction filter.")
@app_commands.autocomplete(faction=faction_autocomplete)
async def top5(interaction, faction: Optional[str] = None):
    await interaction.response.defer()
    await interaction.followup.send(embed=top_embed(await fetch(), 5, faction))


@tree.command(name="top10", description="Show top 10 players.")
@app_commands.describe(faction="Optional faction filter.")
@app_commands.autocomplete(faction=faction_autocomplete)
async def top10(interaction, faction: Optional[str] = None):
    await interaction.response.defer()
    await interaction.followup.send(embed=top_embed(await fetch(), 10, faction))


@tree.command(name="topfactions", description="Show all factions ranked.")
async def topfactions(interaction):
    await interaction.response.defer()
    await interaction.followup.send(embed=factions_embed(await fetch()))


@tree.command(name="raid", description="Show overall raid status.")
async def raid(interaction):
    await interaction.response.defer()
    await interaction.followup.send(embed=raid_embed(await fetch()))


@tree.command(name="stats", description="Show stats for a faction.")
@app_commands.describe(faction="Faction to inspect.")
@app_commands.autocomplete(faction=faction_autocomplete)
async def stats(interaction, faction: str):
    await interaction.response.defer()
    await interaction.followup.send(embed=stats_embed(await fetch(), faction))


@tree.command(name="gap", description="Show the points gap to the faction above.")
@app_commands.describe(faction="Optional; defaults to server faction.")
@app_commands.autocomplete(faction=faction_autocomplete)
async def gap(interaction, faction: Optional[str] = None):
    faction = configured_faction(interaction.guild_id, faction)
    if not faction:
        await interaction.response.send_message("Choose a faction or use `/setup`.", ephemeral=True)
        return
    await interaction.response.defer()
    await interaction.followup.send(embed=gap_embed(await fetch(), faction))


@tree.command(name="intel", description="Show tactical faction intel.")
@app_commands.describe(faction="Optional; defaults to server faction.")
@app_commands.autocomplete(faction=faction_autocomplete)
async def intel(interaction, faction: Optional[str] = None):
    faction = configured_faction(interaction.guild_id, faction)
    if not faction:
        await interaction.response.send_message("Choose a faction or use `/setup`.", ephemeral=True)
        return
    await interaction.response.defer()
    await interaction.followup.send(embed=intel_embed(await fetch(), faction))


@tree.command(name="disable", description="Disable automatic attack alerts.")
@app_commands.checks.has_permissions(manage_guild=True)
async def disable(interaction):
    set_alerts(interaction.guild_id, False)
    await interaction.response.send_message(
        "⚡ Automatic attack alerts disabled. Daily tracking stays ON.",
        ephemeral=True,
    )


@tree.command(name="enable", description="Enable automatic attack alerts.")
@app_commands.checks.has_permissions(manage_guild=True)
async def enable(interaction):
    if str(interaction.guild_id) not in configs:
        await interaction.response.send_message("Use `/setup` first.", ephemeral=True)
        return
    set_alerts(interaction.guild_id, True)
    await interaction.response.send_message("⚡ Automatic attack alerts enabled.", ephemeral=True)


@tasks.loop(seconds=POLL_SECONDS)
async def monitor():
    try:
        data = await fetch()
        events = record_daily(data)
        if not events:
            return

        for gid, c in list(configs.items()):
            if not c.get("alerts", True):
                continue
            wanted = c["faction"].strip().lower()
            relevant = [e for e in events if e["faction"].strip().lower() == wanted]
            if not relevant:
                continue
            guild = client.get_guild(iv(gid))
            if not guild:
                continue
            channel = guild.get_channel(iv(c["channel_id"]))
            if not channel:
                continue
            try:
                await channel.send(embed=activity_embed(c["faction"], relevant))
            except Exception as ex:
                print("Activity post error:", gid, type(ex).__name__, ex)
    except Exception as ex:
        print("Monitor error:", type(ex).__name__, ex)


@monitor.before_loop
async def before_monitor():
    await client.wait_until_ready()


async def sync_commands():
    for guild in client.guilds:
        try:
            tree.clear_commands(guild=guild)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"Guild sync: {guild.name} -> {len(synced)} commands")
        except Exception as ex:
            print("Guild sync error:", guild.id, type(ex).__name__, ex)
    try:
        tree.clear_commands(guild=None)
        await tree.sync()
        print("Legacy global commands cleared.")
    except Exception as ex:
        print("Global cleanup error:", type(ex).__name__, ex)


@client.event
async def on_ready():
    load_configs()
    print(f"{BOT_NAME} v{VERSION} | Logged in as {client.user} | Servers: {len(client.guilds)}")
    print(f"API: {API_URL}")
    print(f"Timezone: UTC | DB: {DB_FILE} | Poll: {POLL_SECONDS}s")
    await sync_commands()
    if not monitor.is_running():
        monitor.start()


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

client.run(TOKEN)
