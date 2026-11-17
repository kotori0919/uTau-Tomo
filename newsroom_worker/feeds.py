"""Bounded RSS/Atom discovery across publishers and news search feeds."""
from __future__ import annotations

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp

from .models import Article, SourceBatch


def plain_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def parse_feed(payload: bytes, source: str, label: str) -> list[Article]:
    root = ET.fromstring(payload)
    rows = root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry")
    articles = []
    for row in rows[:30]:
        fields = {node.tag.rsplit("}", 1)[-1]: node for node in row}

        def text(name: str) -> str:
            node = fields.get(name)
            return "" if node is None else "".join(node.itertext()).strip()

        link_node = fields.get("link")
        url = text("link")
        if link_node is not None and link_node.get("href"):
            url = link_node.get("href", "")
        if not url.startswith(("https://", "http://")):
            continue
        raw_date = text("pubDate") or text("published") or text("updated")
        try:
            date = parsedate_to_datetime(raw_date)
        except (ValueError, TypeError):
            try:
                date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                continue
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        body = plain_text(text("encoded") or text("content") or text("description") or text("summary"))
        title = plain_text(text("title"))
        if not title:
            continue
        articles.append(Article(source=source, url=url, title=title, body=body or title,
                                published_at=date.isoformat(), section="news",
                                meta={"source_label": label}))
    return articles


async def collect_feeds(session: aiohttp.ClientSession, feeds: list[dict]) -> list[SourceBatch]:
    semaphore = asyncio.Semaphore(4)

    async def collect(feed: dict) -> SourceBatch:
        source = "feed_" + feed["id"]
        async with semaphore:
            try:
                async with session.get(feed["url"], timeout=aiohttp.ClientTimeout(total=15),
                                       headers={"User-Agent": "STN-Newsroom/2.0"}) as response:
                    response.raise_for_status()
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > 2_000_000:
                            raise ValueError("feed exceeds 2MB")
                        chunks.append(chunk)
                    payload = b"".join(chunks)
                articles = parse_feed(payload, source, feed["label"])
                return SourceBatch(source, articles, "ok" if articles else "parse_empty")
            except Exception as exc:
                return SourceBatch(source, [], "error", f"{type(exc).__name__}: {str(exc)[:180]}")

    return list(await asyncio.gather(*(collect(feed) for feed in feeds)))
