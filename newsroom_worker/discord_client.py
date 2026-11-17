from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from .config import Settings
from .models import Article


log = logging.getLogger(__name__)
DISCORD_API = "https://discord.com/api/v10"
SOURCE_LABELS = {
    "samsung_display": "삼성디스플레이 뉴스룸",
    "samsung_semiconductor": "삼성전자 반도체 뉴스룸",
    "threads_choi_openai": "Threads @choi.openai",
}


def _escape_title(value: str) -> str:
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def format_news_message(article: Article, summary: str) -> str:
    source = str(article.meta.get("source_label") or SOURCE_LABELS.get(article.source, article.source))
    return (
        f"**{_escape_title(article.title)}**\n"
        f"{summary}\n"
        f"{article.url}\n\n"
        f"출처: {source} ({article.date_label})"
    )


def format_commit_message(article: Article, summary: str, repo: str) -> str:
    sha = str(article.meta.get("sha", ""))[:7]
    files = int(article.meta.get("files", 0))
    additions = int(article.meta.get("additions", 0))
    deletions = int(article.meta.get("deletions", 0))
    label = " · 문서" if article.meta.get("documentation_only") else ""
    return (
        f"**dimigo-HD-Music-Bot 업데이트 · {sha}{label}**\n"
        f"{summary}\n"
        f"변경: 파일 {files}개 (+{additions} / −{deletions})\n"
        f"{article.url}\n\n"
        f"출처: GitHub {repo} ({article.date_label})"
    )


def format_release_message(article: Article, summary: str, repo: str) -> str:
    tag = _escape_title(str(article.meta.get("tag") or article.title))
    return (
        f"**dimigo-HD-Music-Bot 릴리스 · {tag}**\n"
        f"{summary}\n"
        f"{article.url}\n\n"
        f"출처: GitHub {repo} ({article.date_label})"
    )


class DiscordPublisher:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self.session = session
        self._last_post: dict[str, float] = {}

    async def post(self, channel_id: str, content: str) -> bool:
        if len(content) > 2000:
            raise ValueError("Discord message exceeds 2000 characters")
        if self.settings.dry_run:
            log.info("Dry-run Discord post channel=%s content=%s", channel_id, content)
            return True

        elapsed = time.monotonic() - self._last_post.get(channel_id, 0.0)
        if elapsed < 12:
            await asyncio.sleep(12 - elapsed)
        headers = {
            "Authorization": f"Bot {self.settings.discord_token}",
            "Content-Type": "application/json",
        }
        payload = {"content": content, "allowed_mentions": {"parse": []}}
        for attempt in range(2):
            async with self.session.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                json=payload,
            ) as response:
                if 200 <= response.status < 300:
                    self._last_post[channel_id] = time.monotonic()
                    return True
                data = await response.json(content_type=None)
                if response.status == 429:
                    delay = float(data.get("retry_after", 1.0))
                    log.warning("Discord rate limit channel=%s retry_after=%s", channel_id, delay)
                    await asyncio.sleep(delay)
                    continue
                if response.status >= 500 and attempt == 0:
                    await asyncio.sleep(2)
                    continue
                log.error(
                    "Discord post failed channel=%s status=%s response=%s",
                    channel_id,
                    response.status,
                    str(data)[:300],
                )
                return False
        return False

    async def channel_status(self, channel_id: str) -> tuple[int, str]:
        headers = {"Authorization": f"Bot {self.settings.discord_token}"}
        async with self.session.get(
            f"{DISCORD_API}/channels/{channel_id}", headers=headers
        ) as response:
            data = await response.json(content_type=None)
            name = data.get("name", "unknown") if isinstance(data, dict) else "unknown"
            return response.status, str(name)

    async def effective_channel_permissions(self, channel_id: str) -> dict[str, bool]:
        guild_id = str(self.settings.config["guildId"])
        headers = {"Authorization": f"Bot {self.settings.discord_token}"}

        async def get(path: str):
            async with self.session.get(f"{DISCORD_API}{path}", headers=headers) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"Discord HTTP {response.status}: {str(data)[:200]}")
                return data

        current_user = await get("/users/@me")
        user_id = str(current_user["id"])
        member = await get(f"/guilds/{guild_id}/members/{user_id}")
        roles = await get(f"/guilds/{guild_id}/roles")
        channel = await get(f"/channels/{channel_id}")
        role_permissions = {
            str(role["id"]): int(role["permissions"])
            for role in roles
            if isinstance(role, dict)
        }
        permissions = role_permissions.get(guild_id, 0)
        member_roles = {str(role_id) for role_id in member.get("roles", [])}
        for role_id in member_roles:
            permissions |= role_permissions.get(role_id, 0)
        if permissions & (1 << 3):
            permissions = (1 << 53) - 1
        else:
            overwrites = channel.get("permission_overwrites", [])
            everyone = next(
                (row for row in overwrites if str(row.get("id")) == guild_id), None
            )
            if everyone:
                permissions = (permissions & ~int(everyone["deny"])) | int(
                    everyone["allow"]
                )
            deny = 0
            allow = 0
            for row in overwrites:
                if int(row.get("type", -1)) == 0 and str(row.get("id")) in member_roles:
                    deny |= int(row["deny"])
                    allow |= int(row["allow"])
            permissions = (permissions & ~deny) | allow
            member_overwrite = next(
                (
                    row
                    for row in overwrites
                    if int(row.get("type", -1)) == 1
                    and str(row.get("id")) == user_id
                ),
                None,
            )
            if member_overwrite:
                permissions = (permissions & ~int(member_overwrite["deny"])) | int(
                    member_overwrite["allow"]
                )
        view = bool(permissions & (1 << 10))
        return {
            "view": view,
            "send": view and bool(permissions & (1 << 11)),
            "embed": view and bool(permissions & (1 << 14)),
            "history": view and bool(permissions & (1 << 16)),
            "manage_channels": bool(permissions & (1 << 4)),
        }
