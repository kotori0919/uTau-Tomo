from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .pipeline import NewsroomPipeline


log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def seconds_until_news_run(now: datetime | None = None) -> float:
    current = now or datetime.now(KST)
    target = current.replace(hour=8, minute=30, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    jitter = random.uniform(-300, 300)
    return max(0.0, (target - current).total_seconds() + jitter)


class Scheduler:
    def __init__(self, pipeline: NewsroomPipeline) -> None:
        self.pipeline = pipeline
        self.lock = asyncio.Lock()

    async def _run_news(self) -> None:
        async with self.lock:
            try:
                result = await self.pipeline.run_news()
                log.info("News run completed result=%s", result)
            except Exception:
                log.exception("News run failed")

    async def _news_loop(self) -> None:
        await self._run_news()
        while True:
            delay = seconds_until_news_run()
            log.info("Next news run in %.0f seconds", delay)
            await asyncio.sleep(delay)
            await self._run_news()

    async def run(self) -> None:
        await self._news_loop()
