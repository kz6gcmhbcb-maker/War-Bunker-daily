import discord
from datetime import datetime
VERSION = "2.0.0"
BOT_NAME = "WChronicles RaidOps"

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



def time_only(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M:%S") if value else "—"

def factions(data):
    return sorted(data["factions"], key=lambda f: iv(f.get("rank")) or 999999)

def get_faction(data, name):
    return next((f for f in data["factions"] if f["factionName"].casefold() == name.strip().casefold()), None)

def players(data, faction=None):
    return sorted((p for p in data["entries"] if not faction or pfaction(p).casefold() == faction.casefold()), key=lambda p: (ppoints(p), pdamage(p), pattacks(p)), reverse=True)

def activity_embed(faction, events):
    e = discord.Embed(title="⚡ RAID ACTIVITY", description=f"Observed increases for **{faction}**", color=0x42F5C5)
    chunk = ""
    for event in events:
        line = f"• **{event['name']}** +{event['delta']} attack(s) • {time_only(event['detected_at'])} UTC\n"
        if len(chunk) + len(line) > 1000:
            e.add_field(name="ACTIVITY", value=chunk, inline=False)
            chunk = ""
        chunk += line
    if chunk:
        e.add_field(name="ACTIVITY", value=chunk, inline=False)
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
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • LIVE API • player roster may be incomplete")
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
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • LIVE API • player roster may be incomplete")
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
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • LIVE API • player roster may be incomplete")
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
    e.set_footer(text=f"{BOT_NAME} v{VERSION} • LIVE API • player roster may be incomplete")
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


