"""Validated snapshots, raid-scoped counters and transactional alert delivery."""
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{label} must be a nonnegative integer')
    return value


def validate(data):
    if not isinstance(data, dict) or not isinstance(data.get('raidId'), str) or not data['raidId']:
        raise ValueError('Missing stable raidId')
    for key in ('entries', 'factions'):
        if not isinstance(data.get(key), list):
            raise ValueError(f'Missing {key} array')
    seen = set()
    for p in data['entries']:
        if not isinstance(p, dict) or not isinstance(p.get('uid'), str) or not p['uid']:
            raise ValueError('Missing stable player uid')
        if p['uid'] in seen:
            raise ValueError('Duplicate player uid')
        seen.add(p['uid'])
        integer(p.get('attacks'), 'attacks')
        if not isinstance(p.get('factionName'), str) or not p['factionName'].strip():
            raise ValueError('Missing player faction')
        for key in ('totalDamage', 'factionPoints'):
            integer(p.get(key), key)
    for f in data['factions']:
        if not isinstance(f, dict) or not isinstance(f.get('factionName'), str) or not f['factionName'].strip():
            raise ValueError('Missing faction name')
        for key in ('points', 'damage', 'attacks', 'players', 'rank', 'wins'):
            integer(f.get(key), key)
    return data


@dataclass(frozen=True)
class Settings:
    token: str
    api_url: str
    poll_seconds: int
    db_file: Path
    timezone: ZoneInfo

    @classmethod
    def load(cls):
        token = os.getenv('DISCORD_TOKEN', '').strip()
        if not token:
            raise ValueError('DISCORD_TOKEN is required')
        url = os.getenv('API_URL', 'https://chronicles.wfitapp.xyz/api/raid/leaderboard?limit=60')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'chronicles.wfitapp.xyz' or parsed.username or parsed.password:
            raise ValueError('API_URL must use HTTPS on chronicles.wfitapp.xyz')
        poll = int(os.getenv('POLL_SECONDS', '60'))
        if not 20 <= poll <= 3600:
            raise ValueError('POLL_SECONDS must be 20–3600')
        return cls(token, url, poll, Path(os.getenv('DB_FILE', '/data/war_bunker.sqlite3')), ZoneInfo(os.getenv('DAILY_TIMEZONE', 'UTC')))


class Store:
    def __init__(self, path, tz=ZoneInfo('UTC')):
        self.path, self.tz = Path(path), tz
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS tracker_v2 (
              guild_id TEXT, faction TEXT COLLATE NOCASE, channel_id TEXT, alerts INTEGER NOT NULL,
              PRIMARY KEY(guild_id,faction));
            CREATE TABLE IF NOT EXISTS counters_v2 (
              raid TEXT, uid TEXT, name TEXT, faction TEXT, high INTEGER, last_seen TEXT,
              PRIMARY KEY(raid,uid));
            CREATE TABLE IF NOT EXISTS roster_v2 (
              raid TEXT, day TEXT, uid TEXT, name TEXT, faction TEXT, last_seen TEXT,
              PRIMARY KEY(raid,day,uid));
            CREATE TABLE IF NOT EXISTS events_v2 (
              id INTEGER PRIMARY KEY, raid TEXT, day TEXT, uid TEXT, name TEXT,
              faction TEXT, delta INTEGER, detected_at TEXT);
            CREATE INDEX IF NOT EXISTS events_day_faction ON events_v2(day,faction);
            CREATE TABLE IF NOT EXISTS delivery_v2 (
              id INTEGER PRIMARY KEY, event_id INTEGER, guild_id TEXT, faction TEXT, channel_id TEXT,
              attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
              UNIQUE(event_id,guild_id,faction));
            CREATE TABLE IF NOT EXISTS meta_v2 (key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS notification_v2 (
              guild_id TEXT, user_id TEXT, PRIMARY KEY(guild_id,user_id));
            ''')
            if not c.execute("SELECT 1 FROM meta_v2 WHERE key='migration'").fetchone():
                exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guild_config'").fetchone()
                if exists:
                    c.execute('INSERT OR IGNORE INTO tracker_v2 SELECT guild_id, faction, channel_id, alerts FROM guild_config')
                c.execute("INSERT INTO meta_v2 VALUES('migration','1')")

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA busy_timeout=5000')
            with c:
                yield c
        finally:
            c.close()

    def trackers(self, guild=None):
        with self.connection() as c:
            return [dict(r) for r in c.execute('SELECT * FROM tracker_v2' + (' WHERE guild_id=?' if guild is not None else ''), (str(guild),) if guild is not None else ())]

    def setup(self, guild, channel, faction):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = c.execute('SELECT faction FROM tracker_v2 WHERE guild_id=?', (str(guild),)).fetchall()
            if len(rows) >= 6 and faction.casefold() not in [r['faction'].casefold() for r in rows]:
                raise ValueError('Maximum six trackers per server')
            c.execute('INSERT INTO tracker_v2 VALUES(?,?,?,1) ON CONFLICT(guild_id,faction) DO UPDATE SET channel_id=excluded.channel_id,alerts=1', (str(guild), faction, str(channel)))
            # Old queued deliveries must not leak to a replaced channel.
            c.execute('DELETE FROM delivery_v2 WHERE guild_id=? AND faction=? COLLATE NOCASE', (str(guild), faction))

    def toggle(self, guild, faction=None, enabled=None):
        where = 'guild_id=?' + (' AND faction=? COLLATE NOCASE' if faction else '')
        args = (str(guild), faction) if faction else (str(guild),)
        with self.connection() as c:
            if enabled is None:
                c.execute('DELETE FROM tracker_v2 WHERE ' + where, args)
            else:
                c.execute('UPDATE tracker_v2 SET alerts=? WHERE ' + where, (int(enabled), *args))
            if not enabled:
                c.execute('DELETE FROM delivery_v2 WHERE ' + where, args)

    def set_silent(self, guild_id, user_id, enabled):
        with self.connection() as c:
            if enabled:
                c.execute('DELETE FROM notification_v2 WHERE guild_id=? AND user_id=?', (str(guild_id),str(user_id)))
            else:
                c.execute('INSERT OR IGNORE INTO notification_v2 VALUES(?,?)', (str(guild_id),str(user_id)))

    def subscribers(self, guild_id):
        with self.connection() as c:
            return [int(r['user_id']) for r in c.execute('SELECT user_id FROM notification_v2 WHERE guild_id=? ORDER BY user_id', (str(guild_id),))]

    def ingest(self, data, moment=None):
        validate(data)
        moment = moment or datetime.now(timezone.utc)
        day, stamp = moment.astimezone(self.tz).date().isoformat(), moment.isoformat()
        raid, created, regressions = data['raidId'], [], 0
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            # A different daily timezone would reinterpret persisted dates.
            tz = c.execute("SELECT value FROM meta_v2 WHERE key='timezone'").fetchone()
            if tz and tz['value'] != self.tz.key:
                raise ValueError('Daily timezone changed; use a new DB or restore the original timezone')
            c.execute("INSERT OR IGNORE INTO meta_v2 VALUES('timezone',?)", (self.tz.key,))
            # Carry known participants forward even if the leaderboard omits them.
            c.execute('''INSERT OR IGNORE INTO roster_v2(raid,day,uid,name,faction,last_seen)
              SELECT raid,?,uid,name,faction,last_seen FROM counters_v2 WHERE raid=?''', (day,raid))
            for p in data['entries']:
                uid, faction, current = p['uid'], p['factionName'], p['attacks']
                name = str(p.get('displayName') or p.get('username') or uid)
                old = c.execute('SELECT * FROM counters_v2 WHERE raid=? AND uid=?', (raid, uid)).fetchone()
                delta = max(0, current - old['high']) if old else 0
                regressions += int(old is not None and current < old['high'])
                c.execute('INSERT INTO counters_v2 VALUES(?,?,?,?,?,?) ON CONFLICT(raid,uid) DO UPDATE SET name=excluded.name,faction=excluded.faction,high=MAX(high,excluded.high),last_seen=excluded.last_seen', (raid, uid, name, faction, current, stamp))
                c.execute('INSERT INTO roster_v2 VALUES(?,?,?,?,?,?) ON CONFLICT(raid,day,uid) DO UPDATE SET name=excluded.name,faction=excluded.faction,last_seen=excluded.last_seen', (raid, day, uid, name, faction, stamp))
                if delta:
                    event = {'uid': uid, 'name': name, 'faction': faction, 'delta': delta, 'detected_at': stamp}
                    event_id = c.execute('INSERT INTO events_v2(raid,day,uid,name,faction,delta,detected_at) VALUES(?,?,?,?,?,?,?)', (raid, day, uid, name, faction, delta, stamp)).lastrowid
                    c.execute('INSERT INTO delivery_v2(event_id,guild_id,faction,channel_id) SELECT ?,guild_id,faction,channel_id FROM tracker_v2 WHERE faction=? COLLATE NOCASE AND alerts=1', (event_id, faction))
                    created.append(event)
            for key, value in [('last_success', stamp), ('current_raid', raid), ('regressions', str(regressions)), ('snapshot', json.dumps(data))]:
                c.execute('INSERT INTO meta_v2 VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
        return created

    def meta(self):
        with self.connection() as c:
            return dict(c.execute('SELECT key,value FROM meta_v2').fetchall())

    def daily(self, faction, day):
        with self.connection() as c:
            # Events keep faction-at-detection; roster labels never rewrite historical activity.
            return [dict(r) for r in c.execute('''
            WITH people AS (
              SELECT uid,MAX(name) name,MAX(last_seen) last_seen FROM roster_v2
              WHERE day=? AND faction=? COLLATE NOCASE GROUP BY uid
              UNION ALL
              SELECT uid,MAX(name),MAX(detected_at) FROM events_v2
              WHERE day=? AND faction=? COLLATE NOCASE GROUP BY uid
            ), totals AS (
              SELECT uid,SUM(delta) attacks_today,MAX(detected_at) last_attack_at
              FROM events_v2 WHERE day=? AND faction=? COLLATE NOCASE GROUP BY uid
            ) SELECT p.uid,MAX(p.name) name,MAX(p.last_seen) last_seen,
              COALESCE(t.attacks_today,0) attacks_today,t.last_attack_at
              FROM people p LEFT JOIN totals t ON t.uid=p.uid GROUP BY p.uid
              ORDER BY attacks_today DESC,name COLLATE NOCASE
            ''', (day, faction, day, faction, day, faction))]

    def legacy_daily(self, faction, day):
        with self.connection() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='players_daily'").fetchone():
                return []
            return [dict(r) for r in c.execute("SELECT name,uid,last_seen,attacks_today,NULL last_attack_at FROM players_daily WHERE day=? AND faction=? COLLATE NOCASE ORDER BY attacks_today DESC", (day,faction))]

    def activity(self, faction, day, limit):
        with self.connection() as c:
            return [dict(r) for r in c.execute('SELECT * FROM events_v2 WHERE day=? AND faction=? COLLATE NOCASE ORDER BY id DESC LIMIT ?', (day, faction, limit))]

    def history(self, days):
        with self.connection() as c:
            has_legacy = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='players_daily'").fetchone()
            source = 'SELECT day FROM roster_v2' + (' UNION SELECT day FROM players_daily' if has_legacy else '')
            return [r[0] for r in c.execute('SELECT DISTINCT day FROM (' + source + ') ORDER BY day DESC LIMIT ?', (days,))]

    def pending(self, clock):
        with self.connection() as c:
            return [dict(r) for r in c.execute('''SELECT d.*,e.name,e.delta,e.detected_at FROM delivery_v2 d
              JOIN events_v2 e ON e.id=d.event_id WHERE d.next_try<=? ORDER BY d.id LIMIT 100''', (clock,))]

    def delivered(self, delivery_id):
        with self.connection() as c:
            c.execute('DELETE FROM delivery_v2 WHERE id=?', (delivery_id,))

    def failed(self, delivery_id, attempts, clock):
        with self.connection() as c:
            c.execute('UPDATE delivery_v2 SET attempts=attempts+1,next_try=? WHERE id=?', (clock + min(3600, 30 * 2 ** min(attempts, 7)), delivery_id))
