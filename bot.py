"""Railway worker entry point. Importing does not connect to Discord."""
import asyncio
import logging
import time
from datetime import datetime, timezone, date as Date
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks
from raidops.core import Settings, Store
from raidops.api import RaidAPI
from raidops import views

log = logging.getLogger('raidops')


def safe(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))[:180]


def sanitized(value):
    if isinstance(value, dict):
        return {k: sanitized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitized(v) for v in value]
    return safe(value) if isinstance(value, str) else value


def pages(embed):
    """Keep every message under Discord field and aggregate embed limits."""
    data = embed.to_dict()
    fields = data.pop('fields', [])
    data['title'] = data.get('title', '')[:256]
    if 'description' in data:
        data['description'] = data['description'][:4096]
    page = discord.Embed.from_dict(data)
    for field in fields:
        name, value = field['name'][:256], field['value'][:1024] or '—'
        if len(page.fields) >= 25 or len(page) + len(name) + len(value) > 5800:
            yield page
            page = discord.Embed.from_dict(data)
        page.add_field(name=name, value=value, inline=field.get('inline', False))
    yield page


class RaidBot(commands.Bot):
    def __init__(self, settings):
        super().__init__(command_prefix='!', intents=discord.Intents(guilds=True), allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.store = Store(settings.db_file, settings.timezone)
        self.api = RaidAPI(settings.api_url)
        self.last_error = None
        self.synced_guilds = set()
        self.install_commands()

    def day(self):
        return datetime.now(self.settings.timezone).date().isoformat()

    async def setup_hook(self):
        await self.tree.sync()
        self.monitor.change_interval(seconds=self.settings.poll_seconds)
        self.monitor.start()
        self.delivery.start()

    async def close(self):
        running = [x.get_task() for x in (self.monitor, self.delivery) if x.get_task()]
        self.monitor.cancel()
        self.delivery.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        await self.api.close()
        await super().close()

    async def on_ready(self):
        log.info('RaidOps v2 ready; servers=%d', len(self.guilds))
        # Keep one global command set; remove legacy per-guild copies.
        for guild in self.guilds:
            if guild.id not in self.synced_guilds:
                await self.sync_guild(guild)

    async def sync_guild(self, guild):
        try:
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
            self.synced_guilds.add(guild.id)
            log.info('Command cleanup complete for server %s; global commands retained', guild.id)
        except discord.HTTPException:
            log.warning('Guild command cleanup failed for %s; will retry on next ready', guild.id)

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)

    @tasks.loop(seconds=60)
    async def monitor(self):
        try:
            data = await self.api.fetch(force=True)
            events = await asyncio.to_thread(self.store.ingest, data)
            self.last_error = None
            log.info('Snapshot: raid=%s players=%d events=%d', data['raidId'], len(data['entries']), len(events))
        except Exception as exc:
            self.last_error = type(exc).__name__
            log.warning('Poll failed (%s); counters retained', self.last_error)

    @monitor.before_loop
    async def wait_monitor(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=10)
    async def delivery(self):
        try:
            pending = await asyncio.to_thread(self.store.pending, time.time())
            for item in pending:
                try:
                    channel = self.get_channel(int(item['channel_id'])) or await self.fetch_channel(int(item['channel_id']))
                    if not isinstance(channel, discord.TextChannel) or channel.guild.id != int(item['guild_id']):
                        raise ValueError('Invalid delivery channel')
                    event = sanitized(item)
                    await channel.send(embed=views.activity_embed(safe(item['faction']), [event]))
                    await asyncio.to_thread(self.store.delivered, item['id'])
                except Exception as exc:
                    log.warning('Delivery %s failed (%s)', item['id'], type(exc).__name__)
                    await asyncio.to_thread(self.store.failed, item['id'], item['attempts'], time.time())
        except Exception as exc:
            log.warning('Delivery queue failed (%s)', type(exc).__name__)

    @delivery.before_loop
    async def wait_delivery(self):
        await self.wait_until_ready()

    async def tracker(self, interaction, faction=None):
        trackers = await asyncio.to_thread(self.store.trackers, interaction.guild_id)
        if faction:
            return next((t for t in trackers if t['faction'].casefold() == faction.strip().casefold()), None)
        in_channel = [t for t in trackers if int(t['channel_id']) == interaction.channel_id]
        return in_channel[0] if len(in_channel) == 1 else trackers[0] if len(trackers) == 1 else None

    async def resolve(self, interaction, faction):
        if faction:
            data = await self.api.fetch()
            f = views.get_faction(data, faction)
            if not f:
                raise ValueError('Faction not found. Choose from autocomplete.')
            return f['factionName']
        tracker = await self.tracker(interaction)
        if not tracker:
            raise ValueError('Choose a faction or configure this channel with /setup.')
        return tracker['faction']

    async def send_embed(self, interaction, embed):
        for page in pages(embed):
            await interaction.followup.send(embed=page)

    async def daily_embed(self, faction, view, day):
        rows = await asyncio.to_thread(self.store.daily, faction, day)
        legacy = False
        if not rows:
            rows = await asyncio.to_thread(self.store.legacy_daily, faction, day)
            legacy = bool(rows)
        attacked = sum(r['attacks_today'] > 0 for r in rows)
        shown = [r for r in rows if view == 'all' or (r['attacks_today'] > 0) == (view == 'attacked')]
        e = discord.Embed(title=f'⚡ {safe(faction)} — OBSERVED DAILY ACTIVITY', color=0x42F5C5)
        e.description = f'Date: **{day}** ({self.settings.timezone.key})\nSeen: **{len(rows)}** • With detected increases: **{attacked}**\nZero means **no increase observed**, not confirmed inactivity. First sightings establish a baseline; offline and midnight intervals are credited on detection.'
        if legacy:
            e.description += '\n**Legacy v1 archive — unverified counts; v2 fixes do not apply retroactively.**'
        if not rows:
            e.description += '\nNo observations recorded for this date.'
        meta = await asyncio.to_thread(self.store.meta)
        if day == self.day() and meta.get('snapshot'):
            import json
            data = json.loads(meta['snapshot'])
            e.description += f"\nLast recorded snapshot: {meta.get('last_success', 'unknown')}"
            if self.last_error:
                e.description += '\n⚠️ Latest poll failed; observations may be stale.'
            f = views.get_faction(data, faction)
            visible = len(views.players(data, faction))
            if f:
                e.description += f"\nLatest API coverage: **{visible}/{f['players']}** participants returned."
        chunk = ''
        for r in shown:
            stamp = datetime.fromisoformat(r['last_seen']).astimezone(self.settings.timezone).strftime('%Y-%m-%d %H:%M:%S')
            line = f"**{safe(r['name'])}** — {r['attacks_today']} detected • last seen {stamp}\n"
            if len(chunk) + len(line) > 1000:
                e.add_field(name='PLAYERS', value=chunk, inline=False)
                chunk = ''
            chunk += line
        if chunk:
            e.add_field(name='PLAYERS', value=chunk, inline=False)
        e.set_footer(text='RaidOps v2 • Detection times, partial API roster • /status for freshness')
        return e

    def install_commands(self):
        tree = self.tree

        async def autocomplete(interaction, current):
            try:
                # Never wait on the network within Discord's autocomplete deadline.
                data = self.api.snapshot
                if data is None:
                    import json
                    data = json.loads((await asyncio.to_thread(self.store.meta)).get('snapshot', '{}'))
                return [app_commands.Choice(name=f['factionName'][:100], value=f['factionName'][:100]) for f in data.get('factions', []) if current.casefold() in f['factionName'].casefold()][:25]
            except Exception:
                return []

        @tree.error
        async def command_error(interaction, error):
            original = getattr(error, 'original', error)
            if isinstance(error, app_commands.MissingPermissions):
                message = 'Manage Server permission is required.'
            elif isinstance(original, ValueError):
                message = str(original)[:500]
            else:
                message = 'The request failed. Please retry; /status shows tracking health.'
                log.warning('Command failed (%s)', type(original).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

        @tree.command(name='setup', description='Configure one of six faction trackers.')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.autocomplete(faction=autocomplete)
        async def setup(interaction: discord.Interaction, channel: discord.TextChannel, faction: str):
            await interaction.response.defer(ephemeral=True)
            if channel.guild.id != interaction.guild_id:
                raise ValueError('Choose a channel in this server.')
            permissions = channel.permissions_for(interaction.guild.me)
            if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links)):
                raise ValueError('Bot needs View Channel, Send Messages and Embed Links in this channel.')
            name = await self.resolve(interaction, faction)
            await asyncio.to_thread(self.store.setup, interaction.guild_id, channel.id, name)
            await interaction.followup.send(f'Tracking **{safe(name)}** in {channel.mention}. Alerts ON; daily observation ON.', ephemeral=True)

        @tree.command(name='status', description='Show configuration, freshness and queue health.')
        @app_commands.guild_only()
        async def status(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            trackers = await asyncio.to_thread(self.store.trackers, interaction.guild_id)
            meta = await asyncio.to_thread(self.store.meta)
            last = meta.get('last_success')
            age = int((datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()) if last else None
            with self.store.connection() as c:
                queued = c.execute('SELECT COUNT(*) FROM delivery_v2 WHERE guild_id=?', (str(interaction.guild_id),)).fetchone()[0]
            lines = [f"• {safe(t['faction'])} → <#{t['channel_id']}> • alerts {'ON' if t['alerts'] else 'OFF'}" for t in trackers]
            await interaction.followup.send('**RaidOps v2**\n' + ('\n'.join(lines) or 'No trackers. Use /setup.') + f'\nTimezone: {self.settings.timezone.key}\nLast recorded snapshot: {last or "never"} • age {age if age is not None else "unknown"}s\nPoll error: {self.last_error or "none"} • counter regressions in last poll: {meta.get("regressions", "0")}\nQueued alerts: {queued}\nDaily figures are observed increases; API coverage may be incomplete.', ephemeral=True)

        @tree.command(name='daily', description='Observed daily activity; missing means no increase detected.')
        @app_commands.guild_only()
        @app_commands.autocomplete(faction=autocomplete)
        @app_commands.choices(view=[app_commands.Choice(name=x, value=x) for x in ('all', 'attacked', 'missing')])
        async def daily(interaction: discord.Interaction, faction: Optional[str] = None, view: str = 'all', date: Optional[str] = None):
            await interaction.response.defer()
            target = Date.fromisoformat(date).isoformat() if date else self.day()
            name = faction or await self.resolve(interaction, None)
            await self.send_embed(interaction, await self.daily_embed(name, view, target))

        @tree.command(name='dailyhistory', description='Recent observation dates and faction activity.')
        @app_commands.guild_only()
        async def history(interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 7):
            await interaction.response.defer()
            trackers = await asyncio.to_thread(self.store.trackers, interaction.guild_id)
            if not trackers:
                raise ValueError('Use /setup first.')
            dates = await asyncio.to_thread(self.store.history, days)
            e = discord.Embed(title='⚡ DAILY OBSERVATION HISTORY', color=0x42F5C5)
            for day in dates:
                parts = []
                for t in trackers:
                    rows = await asyncio.to_thread(self.store.daily, t['faction'], day)
                    label = ''
                    if not rows:
                        rows = await asyncio.to_thread(self.store.legacy_daily, t['faction'], day)
                        label = ' (legacy, unverified)' if rows else ''
                    parts.append(f"{safe(t['faction'])}: {sum(r['attacks_today'] > 0 for r in rows)}/{len(rows)} with observed increases{label}")
                e.add_field(name=day, value='\n'.join(parts), inline=False)
            e.set_footer(text=f'{self.settings.timezone.key} • v2 history starts with first v2 observation')
            if not dates:
                e.description = 'No v2 history yet.'
            await self.send_embed(interaction, e)

        @tree.command(name='activity', description='Recent detected attack increases.')
        @app_commands.guild_only()
        @app_commands.autocomplete(faction=autocomplete)
        async def activity(interaction: discord.Interaction, faction: Optional[str] = None, limit: app_commands.Range[int, 1, 20] = 10):
            await interaction.response.defer()
            name = faction or await self.resolve(interaction, None)
            events = await asyncio.to_thread(self.store.activity, name, self.day(), limit)
            e = views.activity_embed(safe(name), sanitized(events))
            if not events:
                e.description = 'No increases observed today.'
            await self.send_embed(interaction, e)

        @tree.command(name='update', description='Post the leaderboard to a configured channel.')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.autocomplete(faction=autocomplete)
        async def update(interaction: discord.Interaction, faction: Optional[str] = None):
            await interaction.response.defer(ephemeral=True)
            tracker = await self.tracker(interaction, faction)
            if not tracker:
                raise ValueError('Choose a configured faction.')
            channel = self.get_channel(int(tracker['channel_id']))
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != interaction.guild_id:
                raise ValueError('Configured channel is unavailable; run /setup again.')
            for page in pages(views.factions_embed(sanitized(await self.api.fetch()))):
                await channel.send(embed=page)
            await interaction.followup.send('Live leaderboard posted.', ephemeral=True)

        def install_live(name, description, render, mode='none'):
            async def callback(interaction: discord.Interaction, faction: Optional[str] = None):
                await interaction.response.defer()
                data = await self.api.fetch()
                chosen = await self.resolve(interaction, faction) if mode == 'default' else faction
                # Resolve on raw data, then render escaped copies.
                e = render(sanitized(data), safe(chosen) if chosen else None)
                await self.send_embed(interaction, e)
            callback.__name__ = name
            command = app_commands.Command(name=name, description=description, callback=callback)
            command.guild_only = True
            command.autocomplete('faction')(autocomplete)
            tree.add_command(command)

        install_live('top5', 'Top five visible leaderboard players.', lambda d, f: views.top_embed(d, 5, f))
        install_live('top10', 'Top ten visible leaderboard players.', lambda d, f: views.top_embed(d, 10, f))
        install_live('topfactions', 'All factions ranked.', lambda d, f: views.factions_embed(d))
        install_live('raid', 'Current raid overview.', lambda d, f: views.raid_embed(d))
        install_live('stats', 'Faction statistics.', views.stats_embed, 'default')
        install_live('gap', 'Gap to the faction above.', views.gap_embed, 'default')
        install_live('intel', 'Faction targets and visible top players.', views.intel_embed, 'default')

        def install_toggle(name, enabled):
            async def callback(interaction: discord.Interaction, faction: Optional[str] = None):
                await interaction.response.defer(ephemeral=True)
                if faction and not await self.tracker(interaction, faction):
                    raise ValueError('That faction is not configured here.')
                if enabled is None and not faction:
                    raise ValueError('Specify the faction to remove.')
                await asyncio.to_thread(self.store.toggle, interaction.guild_id, faction, enabled)
                await interaction.followup.send(f'{name}: {safe(faction) if faction else "all trackers"}. Daily observations continue.', ephemeral=True)
            callback.__name__ = name
            callback = app_commands.guild_only()(callback)
            callback = app_commands.default_permissions(manage_guild=True)(callback)
            callback = app_commands.checks.has_permissions(manage_guild=True)(callback)
            command = app_commands.Command(name=name, description=f'{name.capitalize()} automatic alerts or tracker.', callback=callback)
            command.autocomplete('faction')(autocomplete)
            tree.add_command(command)
        install_toggle('enable', True)
        install_toggle('disable', False)
        install_toggle('removetracker', None)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = Settings.load()
    # One worker per database. Railway must also have only one bot service.
    import fcntl
    import signal
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)
    with settings.db_file.with_suffix('.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another worker is using this database') from exc
        async def serve():
            bot = RaidBot(settings)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
            async with bot:
                runner = asyncio.create_task(bot.start(settings.token))
                stopper = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait([runner, stopper], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    await bot.close()
                    stopper.cancel()
                    await asyncio.gather(stopper, return_exceptions=True)
                await runner
        asyncio.run(serve())


if __name__ == '__main__':
    main()
