from __future__ import annotations

import asyncio
import logging

from .pipeline import NewsroomPipeline


log = logging.getLogger(__name__)
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
            delay = int(self.pipeline.settings.config.get("pollSeconds", 600))
            log.info("Next news run in %.0f seconds", delay)
            await asyncio.sleep(delay)
            await self._run_news()

    async def run(self) -> None:
        await self._news_loop()
