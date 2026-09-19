import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

API_URL = os.getenv(
    "API_URL",
    "https://chronicles.wfitapp.xyz/api/raid/leaderboard?limit=25",
)
TOKEN = os.getenv("DISCORD_TOKEN")
POLL_SECONDS = max(20, int(os.getenv("POLL_SECONDS", "60")))
TIMEZONE = os.getenv("DAILY_TIMEZONE", "Europe/Sofia")
DB_FILE = Path(os.getenv("DB_FILE", "war_bunker.sqlite3"))

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True

client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree
configs = {}


def now():
    return datetime.now(ZoneInfo(TIMEZONE))


def day_key():
    return now().date().isoformat()


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
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
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_meta (
            day TEXT PRIMARY KEY,
            started_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            faction TEXT NOT NULL,
            alerts INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()
    return conn


def load_configs():
    global configs
    with get_db() as conn:
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
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO guild_config(guild_id, channel_id, faction, alerts)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                faction=excluded.faction
            """,
            (str(guild_id), str(channel_id), faction),
        )
        conn.commit()

    configs[str(guild_id)] = {
        "channel_id": str(channel_id),
        "faction": faction,
        "alerts": True,
    }


def set_alerts(guild_id, enabled):
    with get_db() as conn:
        conn.execute(
            "UPDATE guild_config SET alerts=? WHERE guild_id=?",
            (int(enabled), str(guild_id)),
        )
        conn.commit()

    if str(guild_id) in configs:
        configs[str(guild_id)]["alerts"] = enabled


def iv(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def fmt(value):
    return f"{iv(value):,}"


def pname(player):
    return str(
        player.get("displayName")
        or player.get("username")
        or player.get("name")
        or player.get("uid")
        or "Unknown"
    )


def puid(player):
    return str(player.get("uid") or pname(player))


def pfaction(player):
    return str(player.get("factionName") or "Unknown")


def pattacks(player):
    return iv(player.get("attacks"))


def ppoints(player):
    return iv(player.get("factionPoints", player.get("points")))


def pdamage(player):
    return iv(player.get("totalDamage", player.get("damage")))


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
            return await response.json()


def factions(data):
    return sorted(
        data.get("factions", []),
        key=lambda f: iv(f.get("rank")) or 999999,
    )


def get_faction(data, name):
    q = str(name or "").strip().lower()
    return next(
        (
            f
            for f in data.get("factions", [])
            if str(f.get("factionName", "")).strip().lower() == q
        ),
        None,
    )


def players(data, faction=None):
    rows = list(data.get("entries", []))

    if faction:
        q = faction.strip().lower()
        rows = [
            p for p in rows
            if pfaction(p).strip().lower() == q
        ]

    return sorted(
        rows,
        key=lambda p: (ppoints(p), pdamage(p), pattacks(p)),
        reverse=True,
    )


def ensure_daily_meta(conn, day):
    row = conn.execute(
        "SELECT started_at FROM daily_meta WHERE day=?",
        (day,),
    ).fetchone()

    if row:
        return row["started_at"]

    started = now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO daily_meta(day, started_at) VALUES (?, ?)",
        (day, started),
    )
    return started


def record_daily(data):
    """
    Track the delta of the cumulative player `attacks` counter.

    Returns attack events observed during this poll:
    [(uid, name, faction, delta), ...]
    """
    today = day_key()
    timestamp = now().isoformat(timespec="seconds")
    events = []

    with get_db() as conn:
        ensure_daily_meta(conn, today)

        for player in data.get("entries", []):
            player_uid = puid(player)
            name = pname(player)
            faction = pfaction(player)
            current = pattacks(player)

            row = conn.execute(
                """
                SELECT *
                FROM players_daily
                WHERE day=? AND uid=?
                """,
                (today, player_uid),
            ).fetchone()

            if row is None:
                conn.execute(
                    """
                    INSERT INTO players_daily
                    (day, uid, name, faction, start_attacks,
                     last_attacks, attacks_today, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        today,
                        player_uid,
                        name,
                        faction,
                        current,
                        current,
                        timestamp,
                    ),
                )
                continue

            previous = iv(row["last_attacks"])
            delta = max(0, current - previous)

            if delta:
                events.append(
                    {
                        "uid": player_uid,
                        "name": name,
                        "faction": faction,
                        "delta": delta,
                    }
                )

            conn.execute(
                """
                UPDATE players_daily
                SET name=?,
                    faction=?,
                    last_attacks=?,
                    attacks_today=attacks_today+?,
                    last_seen=?
                WHERE day=? AND uid=?
                """,
                (
                    name,
                    faction,
                    current,
                    delta,
                    timestamp,
                    today,
                    player_uid,
                ),
            )

        conn.commit()

    return events


def daily_info(faction):
    today = day_key()

    with get_db() as conn:
        meta = conn.execute(
            "SELECT started_at FROM daily_meta WHERE day=?",
            (today,),
        ).fetchone()

        rows = conn.execute(
            """
            SELECT *
            FROM players_daily
            WHERE day=? AND lower(faction)=lower(?)
            ORDER BY attacks_today DESC, name COLLATE NOCASE
            """,
            (today, faction),
        ).fetchall()

    return (meta["started_at"] if meta else None), rows


def build_daily_embed(faction, view="all"):
    started_at, rows = daily_info(faction)

    attacked = [r for r in rows if iv(r["attacks_today"]) > 0]
    missing = [r for r in rows if iv(r["attacks_today"]) == 0]

    if view == "attacked":
        shown = attacked
        title = f"⚡ {faction.upper()} — ATTACKED TODAY"
    elif view == "missing":
        shown = missing
        title = f"⚡ {faction.upper()} — NOT ATTACKED TODAY"
    else:
        shown = rows
        title = f"⚡ {faction.upper()} — DAILY ATTACK CHECK"

    embed = discord.Embed(title=title, color=0x42F5C5)

    if not rows:
        embed.description = (
            "No players have been captured by the daily tracker yet."
        )
        embed.set_footer(
            text="WAR BUNKER • DAILY TRACKER"
        )
        return embed

    if view == "all":
        embed.description = (
            f"**{len(attacked)}** attacked • "
            f"**{len(missing)}** not attacked\n"
            f"Day: **{day_key()}** • Timezone: **{TIMEZONE}**"
        )
    elif view == "attacked":
        embed.description = (
            f"**{len(attacked)}** player(s) with an observed attack today."
        )
    else:
        embed.description = (
            f"**{len(missing)}** player(s) with no observed attack today."
        )

    lines = []
    for index, row in enumerate(shown, 1):
        count = iv(row["attacks_today"])
        if view == "missing":
            lines.append(f"{index}. **{row['name']}**")
        else:
            lines.append(
                f"{index}. **{row['name']}** — **{count}** attack(s)"
            )

    chunks = []
    current = ""

    for line in lines:
        if len(current) + len(line) + 1 > 1000:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line

    if current:
        chunks.append(current)

    for index, chunk in enumerate(chunks, 1):
        embed.add_field(
            name="PLAYERS" if index == 1 else "CONTINUED",
            value=chunk,
            inline=False,
        )

    coverage = (
        "Tracker started today at "
        f"{started_at.split('T')[1] if started_at and 'T' in started_at else started_at}."
        if started_at
        else "Tracker start time unavailable."
    )

    embed.set_footer(
        text=(
            f"WAR BUNKER • {coverage} "
            "Earlier attacks cannot be reconstructed from leaderboard data."
        )
    )
    return embed


def activity_embed(faction, events):
    embed = discord.Embed(
        title="⚡ RAID ACTIVITY",
        description="\n".join(
            f"• **{event['name']}** made **{event['delta']}** new attack(s)"
            for event in events
        ),
        color=0x42F5C5,
    )

    embed.add_field(
        name="TRACKED FACTION",
        value=f"**{faction}**",
        inline=False,
    )
    embed.set_footer(text="THE CURRENT FINDS ITS OWN. • EVENT-DRIVEN")
    return embed


async def faction_autocomplete(interaction, current):
    try:
        data = await fetch()
        names = [
            str(f.get("factionName", "")).strip()
            for f in data.get("factions", [])
        ]
    except Exception:
        names = []

    q = current.lower().strip()

    return [
        app_commands.Choice(name=name, value=name)
        for name in names
        if name and (not q or q in name.lower())
    ][:25]


def configured_faction(guild_id, faction=None):
    return (
        faction
        or configs.get(str(guild_id), {}).get("faction")
    )


@tree.command(
    name="setup",
    description="Configure channel and faction for War Bunker.",
)
@app_commands.describe(
    channel="Automatic activity channel.",
    faction="Faction to track.",
)
@app_commands.autocomplete(faction=faction_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(interaction, channel: discord.TextChannel, faction: str):
    data = await fetch()
    actual = get_faction(data, faction)

    if not actual:
        await interaction.response.send_message(
            "Faction not found. Choose one from autocomplete.",
            ephemeral=True,
        )
        return

    real_name = str(actual.get("factionName"))
    save_config(interaction.guild_id, channel.id, real_name)

    await interaction.response.send_message(
        f"⚡ **War Bunker configured**\n"
        f"Channel: {channel.mention}\n"
        f"Faction: **{real_name}**\n"
        f"Daily tracker: **ON**\n"
        f"Attack alerts: **ON**",
        ephemeral=True,
    )


@tree.command(name="status", description="Show War Bunker status.")
async def status(interaction):
    config = configs.get(str(interaction.guild_id))

    if not config:
        await interaction.response.send_message(
            "Not configured. Use `/setup`.",
            ephemeral=True,
        )
        return

    channel = interaction.guild.get_channel(
        iv(config["channel_id"])
    )

    await interaction.response.send_message(
        f"Channel: {channel.mention if channel else 'not found'}\n"
        f"Faction: **{config['faction']}**\n"
        f"Attack alerts: **{'ON' if config['alerts'] else 'OFF'}**\n"
        f"Daily timezone: **{TIMEZONE}**",
        ephemeral=True,
    )


@tree.command(
    name="daily",
    description="Show who attacked and who did not attack today.",
)
@app_commands.describe(
    faction="Optional; defaults to server faction.",
    view="all, attacked, or missing.",
)
@app_commands.autocomplete(faction=faction_autocomplete)
@app_commands.choices(
    view=[
        app_commands.Choice(name="all", value="all"),
        app_commands.Choice(name="attacked", value="attacked"),
        app_commands.Choice(name="missing", value="missing"),
    ]
)
async def daily(
    interaction,
    faction: Optional[str] = None,
    view: str = "all",
):
    faction = configured_faction(interaction.guild_id, faction)

    if not faction:
        await interaction.response.send_message(
            "Choose a faction or use `/setup`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    try:
        data = await fetch()
        record_daily(data)
        embed = build_daily_embed(faction, view)
        await interaction.followup.send(embed=embed)
    except Exception as ex:
        await interaction.followup.send(
            f"⚠️ Daily tracker error: `{type(ex).__name__}`"
        )


@tree.command(
    name="update",
    description="Post the current faction leaderboard.",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def update_cmd(interaction):
    config = configs.get(str(interaction.guild_id))

    if not config:
        await interaction.response.send_message(
            "Use `/setup` first.",
            ephemeral=True,
        )
        return

    channel = interaction.guild.get_channel(
        iv(config["channel_id"])
    )

    if not channel:
        await interaction.response.send_message(
            "Configured channel not found.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    data = await fetch()
    embed = discord.Embed(
        title="⚡ FACTION LEADERBOARD",
        color=0x42F5C5,
    )

    for faction in factions(data):
        embed.add_field(
            name=(
                f"#{iv(faction.get('rank'))} "
                f"{faction.get('factionName')}"
            ),
            value=(
                f"Points: **{fmt(faction.get('points'))}** • "
                f"Damage: **{fmt(faction.get('damage'))}**\n"
                f"Attacks: **{fmt(faction.get('attacks'))}** • "
                f"Walkers: **{fmt(faction.get('players'))}** • "
                f"Wins: **{fmt(faction.get('wins'))}**"
            ),
            inline=False,
        )

    await channel.send(embed=embed)
    await interaction.followup.send(
        "⚡ Manual leaderboard posted.",
        ephemeral=True,
    )


@tree.command(
    name="top5",
    description="Show top 5 players.",
)
@app_commands.describe(faction="Optional faction filter.")
@app_commands.autocomplete(faction=faction_autocomplete)
async def top5(interaction, faction: Optional[str] = None):
    await interaction.response.defer()
    data = await fetch()
    rows = players(data, faction)[:5]

    embed = discord.Embed(
        title="⚡ TOP 5 PLAYERS",
        color=0x42F5C5,
    )

    for index, player in enumerate(rows, 1):
        embed.add_field(
            name=f"{index}. {pname(player)}",
            value=(
                f"Points: **{fmt(ppoints(player))}** • "
                f"Damage: **{fmt(pdamage(player))}** • "
                f"Attacks: **{fmt(pattacks(player))}**"
            ),
            inline=False,
        )

    if not rows:
        embed.description = "No players found."

    await interaction.followup.send(embed=embed)


@tree.command(
    name="topfactions",
    description="Show all factions ranked.",
)
async def topfactions(interaction):
    await interaction.response.defer()
    data = await fetch()

    embed = discord.Embed(
        title="⚡ FACTION LEADERBOARD",
        color=0x42F5C5,
    )

    for faction in factions(data):
        embed.add_field(
            name=(
                f"#{iv(faction.get('rank'))} "
                f"{faction.get('factionName')}"
            ),
            value=(
                f"Points: **{fmt(faction.get('points'))}** • "
                f"Damage: **{fmt(faction.get('damage'))}** • "
                f"Attacks: **{fmt(faction.get('attacks'))}**"
            ),
            inline=False,
        )

    await interaction.followup.send(embed=embed)


@tree.command(
    name="stats",
    description="Show stats for a faction.",
)
@app_commands.describe(faction="Faction to inspect.")
@app_commands.autocomplete(faction=faction_autocomplete)
async def stats(interaction, faction: str):
    await interaction.response.defer()
    data = await fetch()
    item = get_faction(data, faction)

    embed = discord.Embed(
        title=f"⚡ {faction.upper()} — STATS",
        color=0x42F5C5,
    )

    if not item:
        embed.description = "Faction not found."
    else:
        embed.description = f"Rank **#{iv(item.get('rank'))}**"
        for label, key in [
            ("POINTS", "points"),
            ("DAMAGE", "damage"),
            ("ATTACKS", "attacks"),
            ("WALKERS", "players"),
            ("WINS", "wins"),
        ]:
            embed.add_field(
                name=label,
                value=fmt(item.get(key)),
                inline=True,
            )

    await interaction.followup.send(embed=embed)


@tree.command(
    name="disable",
    description="Disable automatic attack alerts.",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def disable(interaction):
    set_alerts(interaction.guild_id, False)
    await interaction.response.send_message(
        "⚡ Attack alerts disabled. `/daily` tracking remains active.",
        ephemeral=True,
    )


@tree.command(
    name="enable",
    description="Enable automatic attack alerts.",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def enable(interaction):
    set_alerts(interaction.guild_id, True)
    await interaction.response.send_message(
        "⚡ Attack alerts enabled.",
        ephemeral=True,
    )


@tasks.loop(seconds=POLL_SECONDS)
async def monitor():
    try:
        data = await fetch()
        events = record_daily(data)

        if not events:
            return

        for guild_id, config in list(configs.items()):
            if not config.get("alerts", True):
                continue

            faction = config["faction"].strip().lower()
            relevant = [
                event
                for event in events
                if event["faction"].strip().lower() == faction
            ]

            if not relevant:
                continue

            guild = client.get_guild(iv(guild_id))
            if not guild:
                continue

            channel = guild.get_channel(
                iv(config["channel_id"])
            )
            if not channel:
                continue

            try:
                await channel.send(
                    embed=activity_embed(
                        config["faction"],
                        relevant,
                    )
                )
            except Exception as ex:
                print(
                    "Activity post error:",
                    guild_id,
                    type(ex).__name__,
                    ex,
                )

    except Exception as ex:
        print(
            "Monitor error:",
            type(ex).__name__,
            ex,
        )


@monitor.before_loop
async def before_monitor():
    await client.wait_until_ready()


async def sync_commands():
    """
    Guild-only sync. Clear the old guild command cache first,
    then sync the current definitions. Finally remove old global
    commands left by previous versions.
    """
    for guild in client.guilds:
        try:
            tree.clear_commands(guild=guild)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(
                f"Guild sync: {guild.name} -> "
                f"{len(synced)} commands"
            )
        except Exception as ex:
            print(
                "Guild sync error:",
                guild.id,
                type(ex).__name__,
                ex,
            )

    try:
        tree.clear_commands(guild=None)
        await tree.sync()
        print("Legacy global commands cleared.")
    except Exception as ex:
        print(
            "Global cleanup error:",
            type(ex).__name__,
            ex,
        )


@client.event
async def on_ready():
    load_configs()

    print(
        f"Logged in as {client.user}. "
        f"Servers: {len(client.guilds)}"
    )

    await sync_commands()

    if not monitor.is_running():
        monitor.start()


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

client.run(TOKEN)
