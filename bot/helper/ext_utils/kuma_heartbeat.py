import asyncio
from logging import getLogger

import httpx

from bot.core.config_manager import Config

KUMA_INTERVAL = 60

LOGGER = getLogger(__name__)


async def kuma_heartbeat():
    """Ping the configured Uptime Kuma push URL until the task is cancelled."""
    if not Config.KUMA_URL:
        return

    async with httpx.AsyncClient() as client:
        while True:
            try:
                await client.get(Config.KUMA_URL, timeout=10)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Uptime Kuma heartbeat failed")

            await asyncio.sleep(KUMA_INTERVAL)
