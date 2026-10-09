import asyncio
import time
import aiohttp
from .core import validate


class RaidAPI:
    def __init__(self, url):
        self.url, self.session, self.snapshot = url, None, None
        self.fetched = 0
        self.lock = asyncio.Lock()

    async def close(self):
        if self.session:
            await self.session.close()

    async def fetch(self, force=False):
        async with self.lock:
            if self.snapshot and not force and time.monotonic() - self.fetched < 20:
                return self.snapshot
            if self.session is None:
                self.session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=15), headers={'Accept': 'application/json', 'User-Agent': 'WChronicles-RaidOps/2.0', 'Referer': 'https://chronicles.wfitapp.xyz/'})
            async with self.session.get(self.url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError(f'Raid API HTTP {response.status}')
                if response.content_length and response.content_length > 2_000_000:
                    raise ValueError('API payload too large')
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ValueError("API payload too large")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                import json
                data = validate(json.loads(raw))
            self.snapshot, self.fetched = data, time.monotonic()
            return data
