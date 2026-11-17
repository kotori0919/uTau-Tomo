from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from playwright.async_api import Browser, Page, async_playwright

from .config import Settings
from .models import Article, SourceBatch, canonicalize_url


log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
USER_AGENT = "STN-Newsroom-Worker/1.0 (+https://github.com/kotori0919/uTau-Tomo)"


def _published_at(text: str) -> str | None:
    patterns = (
        r"(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일",
        r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            year, month, day = (int(x) for x in match.groups())
            try:
                return datetime(year, month, day, tzinfo=KST).isoformat()
            except ValueError:
                return None
    return None


def _threads_published_at(value: str) -> str | None:
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return published.isoformat() if published.utcoffset() is not None else None


def _unique_links(rows: list[dict[str, Any]], predicate: Any) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for row in rows:
        href = canonicalize_url(str(row.get("href") or ""))
        if not href or href in seen or not predicate(href):
            continue
        seen.add(href)
        result.append({"href": href, "text": " ".join(str(row.get("text") or "").split())})
    return result


class BrowserSources:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> "BrowserSources":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.browser_headless
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _page(self) -> Page:
        if not self._browser:
            raise RuntimeError("browser is not started")
        context = await self._browser.new_context(
            user_agent=USER_AGENT,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        return await context.new_page()

    async def _goto(self, page: Page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        await page.wait_for_timeout(1_500)

    async def _link_rows(self, page: Page) -> list[dict[str, Any]]:
        return await page.locator("a[href]").evaluate_all(
            """nodes => nodes.map(node => ({
                href: node.href,
                text: (node.innerText || node.getAttribute('aria-label') || '').trim()
            }))"""
        )

    async def _article_detail(
        self,
        page: Page,
        source: str,
        url: str,
        fallback_title: str,
    ) -> Article | None:
        try:
            await self._goto(page, url)
            data = await page.evaluate(
                """() => {
                    const title = document.querySelector('meta[property="og:title"]')?.content
                        || document.querySelector('h1')?.innerText
                        || document.title;
                    const root = document.querySelector('article, main, .view_cont, .article-content, .entry-content, .post-content')
                        || document.body;
                    const section = document.querySelector('.category, .cate, .section, [rel="category tag"]')?.innerText || '';
                    const date = document.querySelector('time')?.getAttribute('datetime')
                        || document.querySelector('time')?.innerText
                        || root.innerText.slice(0, 1000);
                    return {title, body: root.innerText, section, date};
                }"""
            )
            title = unescape(str(data.get("title") or fallback_title)).strip()
            body = str(data.get("body") or "").strip()
            if not title or len(body) < 40:
                return None
            return Article(
                source=source,
                url=url,
                title=title,
                body=body,
                published_at=_published_at(str(data.get("date") or body[:1000])),
                section=str(data.get("section") or ""),
            )
        except Exception as exc:
            log.warning("Article parse failed source=%s url=%s error=%s", source, url, exc)
            return None

    async def _collect_details(
        self,
        parent: Page,
        source: str,
        links: list[dict[str, str]],
    ) -> list[Article]:
        semaphore = asyncio.Semaphore(4)

        async def collect(link: dict[str, str]) -> Article | None:
            async with semaphore:
                page = await parent.context.new_page()
                try:
                    return await self._article_detail(
                        page, source, link["href"], link["text"]
                    )
                finally:
                    await page.close()

        rows = await asyncio.gather(*(collect(link) for link in links))
        return [article for article in rows if article is not None]

    async def collect_samsung_display(self) -> SourceBatch:
        source = "samsung_display"
        page = await self._page()
        try:
            log.info("Collecting source=%s", source)
            home = str(self.settings.config["sources"][source])
            await self._goto(page, home)
            rows = await self._link_rows(page)
            links = _unique_links(
                rows,
                lambda href: urlsplit(href).netloc == "news.samsungdisplay.com"
                and bool(re.fullmatch(r"/\d{4,}", urlsplit(href).path)),
            )
            limit = int(self.settings.config["limits"]["candidateScan"])
            articles = await self._collect_details(page, source, links[:limit])
            log.info("Collected source=%s links=%s articles=%s", source, len(links), len(articles))
            status = "ok" if articles else "parse_empty"
            return SourceBatch(source, articles, status, f"links={len(links)}")
        except Exception as exc:
            return SourceBatch(source, [], "error", str(exc)[:300])
        finally:
            await page.context.close()

    async def collect_samsung_semiconductor(self) -> SourceBatch:
        source = "samsung_semiconductor"
        page = await self._page()
        try:
            log.info("Collecting source=%s", source)
            home = str(self.settings.config["sources"][source])
            await self._goto(page, home)
            rows = await self._link_rows(page)

            def is_article(href: str) -> bool:
                parts = urlsplit(href)
                if parts.netloc != "news.samsungsemiconductor.com":
                    return False
                path = parts.path.rstrip("/")
                if not path.startswith("/kr/") or path == "/kr":
                    return False
                blocked = ("/category/", "/tag/", "/author/", "/page/", "/search")
                return not any(value in path for value in blocked)

            links = _unique_links(rows, is_article)
            limit = int(self.settings.config["limits"]["candidateScan"])
            articles = await self._collect_details(page, source, links[:limit])
            log.info("Collected source=%s links=%s articles=%s", source, len(links), len(articles))
            status = "ok" if articles else "parse_empty"
            return SourceBatch(source, articles, status, f"links={len(links)}")
        except Exception as exc:
            return SourceBatch(source, [], "error", str(exc)[:300])
        finally:
            await page.context.close()

    async def collect_threads(self) -> SourceBatch:
        source = "threads_choi_openai"
        page = await self._page()
        try:
            log.info("Collecting source=%s", source)
            await self._goto(page, str(self.settings.config["sources"][source]))
            dialog_count = await page.locator('[role="dialog"]').count()
            await page.locator('[role="dialog"]').evaluate_all(
                "nodes => nodes.forEach(node => node.remove())"
            )
            rows = await page.locator('a[href*="/post/"]:has(time[datetime])').evaluate_all(
                """nodes => nodes.flatMap(node => {
                    const cardSelector = 'article, [data-pressable-container="true"]';
                    const card = node.closest(cardSelector);
                    const timelineItem = node.closest('[data-pagelet^="threads_profile_posts_timeline_"]');
                    if (card && timelineItem?.contains(card.parentElement?.closest(cardSelector))) {
                        return [];
                    }
                    const threadRoot = timelineItem && Array.from(timelineItem.querySelectorAll(
                        'a[href*="/post/"]:has(time[datetime])'
                    )).find(link => {
                        const linkCard = link.closest(cardSelector);
                        return !timelineItem.contains(linkCard?.parentElement?.closest(cardSelector));
                    });
                    if (threadRoot && threadRoot.pathname !== node.pathname) return [];
                    const time = node.querySelector('time[datetime]').getAttribute('datetime');
                    return [{href: node.href, text: (card || node.parentElement).innerText.trim(), time}];
                })"""
            )
            links = _unique_links(
                rows,
                lambda href: urlsplit(href).hostname in {"threads.com", "www.threads.com"}
                and "/@choi.openai/post/" in urlsplit(href).path,
            )
            by_url: dict[str, dict[str, Any]] = {}
            times: dict[str, str] = {}
            for row in rows:
                url = canonicalize_url(str(row.get("href") or ""))
                if published := _threads_published_at(row["time"]):
                    times[url] = published
                if len(row["text"]) > len(by_url.get(url, {}).get("text", "")) or url not in by_url:
                    by_url[url] = row
            limit = int(self.settings.config["limits"]["candidateScan"])
            articles: list[Article] = []
            for link in links[:limit]:
                row = by_url.get(link["href"], {})
                body = " ".join(str(row.get("text") or link["text"]).split())
                body = re.sub(r"^@?choi\.openai\s+(?:\d+\s*(?:초|분|시간|일)\s*)?", "", body)
                title = body[:120] or "Threads 게시물"
                articles.append(
                    Article(
                        source=source,
                        url=link["href"],
                        title=title,
                        body=body,
                        published_at=times.get(link["href"]),
                        section="AI Threads",
                    )
                )
            if not articles and dialog_count:
                return SourceBatch(source, [], "login_wall", "login dialog blocked timeline")
            log.info("Collected source=%s links=%s articles=%s", source, len(links), len(articles))
            return SourceBatch(
                source,
                articles,
                "ok" if articles else "parse_empty",
                f"links={len(links)} dialogs={dialog_count}",
            )
        except Exception as exc:
            return SourceBatch(source, [], "error", str(exc)[:300])
        finally:
            await page.context.close()

    async def collect_all(self) -> list[SourceBatch]:
        return [
            await self.collect_samsung_display(),
            await self.collect_samsung_semiconductor(),
            await self.collect_threads(),
        ]


class GitHubSource:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self.session = session
        self.repo = settings.github["repo"]
        self.branch = settings.github["branch"]

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        return headers

    async def _get(self, path: str) -> Any:
        async with self.session.get(
            f"https://api.github.com/repos/{self.repo}{path}", headers=self.headers
        ) as response:
            data = await response.json(content_type=None)
            if response.status in {403, 429}:
                raise RuntimeError(f"GitHub rate limited ({response.status})")
            if response.status >= 400:
                raise RuntimeError(f"GitHub HTTP {response.status}: {str(data)[:300]}")
            return data

    async def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        commits = await self._get(f"/commits?per_page=20&sha={self.branch}")
        releases = await self._get("/releases?per_page=5")
        tags = await self._get("/tags?per_page=10")
        return list(commits), list(releases), list(tags)

    async def commit_article(self, sha: str) -> Article:
        detail = await self._get(f"/commits/{sha}")
        commit = detail.get("commit", {})
        message = str(commit.get("message") or "").strip()
        files = detail.get("files") or []
        filenames = [str(item.get("filename") or "") for item in files[:5]]
        stats = detail.get("stats") or {}
        documentation_only = bool(files) and all(
            name.lower().endswith((".md", ".rst", ".txt"))
            or name.lower().startswith("docs/")
            for name in filenames
        )
        body = f"{message}\n변경 파일: {', '.join(filenames)}\n변경량: +{stats.get('additions', 0)} -{stats.get('deletions', 0)}"
        published = str(commit.get("committer", {}).get("date") or "") or None
        return Article(
            source="github_repo",
            url=str(detail.get("html_url") or f"https://github.com/{self.repo}/commit/{sha}"),
            title=message.splitlines()[0] if message else sha[:7],
            body=body,
            published_at=published,
            section="문서" if documentation_only else "commit",
            meta={
                "sha": sha,
                "files": len(files),
                "additions": int(stats.get("additions", 0)),
                "deletions": int(stats.get("deletions", 0)),
                "documentation_only": documentation_only,
                "filenames": filenames,
            },
        )

    def release_article(self, release: dict[str, Any]) -> Article:
        tag = str(release.get("tag_name") or release.get("name") or "release")
        return Article(
            source="github_repo",
            url=str(release.get("html_url") or f"https://github.com/{self.repo}/releases"),
            title=str(release.get("name") or tag),
            body=str(release.get("body") or release.get("name") or tag),
            published_at=str(release.get("published_at") or "") or None,
            section="release",
            meta={"tag": tag, "release": True},
        )
