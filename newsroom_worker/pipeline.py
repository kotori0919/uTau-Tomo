from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

import aiohttp

from .config import Settings
from .discord_client import (
    DiscordPublisher,
    format_commit_message,
    format_news_message,
    format_release_message,
)
from .intelligence import IntelligenceClient
from .models import Article, SourceBatch, canonicalize_url
from .sources import BrowserSources, GitHubSource
from .state import AtomicJsonState, news_state_default, repo_state_default


log = logging.getLogger(__name__)
SECURITY_PATTERN = re.compile(
    r"\bCVE[-\s]?\d|취약점|해킹|랜섬웨어|악성코드|제로데이|보안\s*결함",
    re.IGNORECASE,
)


class NewsroomPipeline:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self.session = session
        self.intelligence = IntelligenceClient(settings, session)
        self.publisher = DiscordPublisher(settings, session)
        seeds = [
            canonicalize_url(str(url))
            for url in settings.config.get("initialPostedUrls", [])
        ]
        self.news_state = AtomicJsonState(
            settings.state_path, news_state_default(seeds)
        )
        self.repo_state = AtomicJsonState(
            settings.repo_state_path,
            repo_state_default(settings.github["repo"], settings.github["branch"]),
        )

    async def _selection_log(self, text: str) -> None:
        try:
            if not await self.publisher.post(
                self.settings.channels["selection_log"], text[:2000]
            ):
                log.warning("Selection log could not be posted")
        except Exception as exc:
            log.warning("Selection log failed: %s", exc)

    async def run_news(self) -> dict[str, Any]:
        state, _healthy = self.news_state.load()
        posted = {canonicalize_url(str(url)) for url in state.get("posted", [])}
        initialized = {str(value) for value in state.get("initializedSources", [])}
        empty_runs = Counter(
            {str(k): int(v) for k, v in dict(state.get("emptyRuns", {})).items()}
        )
        async with BrowserSources(self.settings) as sources:
            batches = await sources.collect_all()

        stats = Counter()
        report: list[str] = []
        pending: list[Article] = []
        per_source_limit = int(self.settings.config["limits"]["newsPerSource"])
        total_limit = int(self.settings.config["limits"]["newsTotal"])

        for batch in batches:
            stats[f"{batch.source}:{batch.status}"] += 1
            if batch.status == "parse_empty":
                empty_runs[batch.source] += 1
                report.append(
                    f"- {batch.source}: 파싱 결과 0건 ({empty_runs[batch.source]}회 연속)"
                )
                if empty_runs[batch.source] == 2:
                    await self._selection_log(
                        f"⚠️ {batch.source} 셀렉터 점검 필요: 2회 연속 수집 결과가 0건입니다."
                    )
                continue
            if batch.status != "ok":
                report.append(f"- {batch.source}: {batch.status} · {batch.detail}")
                continue
            empty_runs[batch.source] = 0
            if batch.source not in initialized:
                posted.update(article.url for article in batch.articles)
                initialized.add(batch.source)
                report.append(
                    f"- {batch.source}: 최초 실행 {len(batch.articles)}건 시드(백필 없음)"
                )
                continue
            unseen = [article for article in batch.articles if article.url not in posted]
            unseen.sort(key=lambda article: article.published_at or "", reverse=True)
            pending.extend(unseen[:per_source_limit])
            report.append(
                f"- {batch.source}: 수집 {len(batch.articles)} · 신규 {len(unseen)}"
            )

        pending.sort(key=lambda article: article.published_at or "", reverse=True)
        for article in pending[:total_limit]:
            if article.source.startswith("samsung_"):
                try:
                    decision = await self.intelligence.classify(article)
                except Exception as exc:
                    stats["classification_failed"] += 1
                    log.error("Classification failed url=%s error=%s", article.url, exc)
                    continue
                log.info(
                    "Knowledge decision source=%s keep=%s knowledge=%.4f promotion=%.4f reason=%s url=%s",
                    article.source,
                    decision.keep,
                    decision.knowledge_score,
                    decision.promotion_score,
                    decision.reason,
                    article.url,
                )
                if not decision.keep:
                    stats["rejected"] += 1
                    continue
            try:
                summary = await self.intelligence.summarize(article)
            except Exception as exc:
                stats["summary_failed"] += 1
                log.error("Summary failed url=%s error=%s", article.url, exc)
                continue
            channel_key = article.source
            if article.source == "threads_choi_openai" and SECURITY_PATTERN.search(
                f"{article.title}\n{article.body}"
            ):
                channel_key = "threads_security"
            message = format_news_message(article, summary)
            if await self.publisher.post(self.settings.channels[channel_key], message):
                posted.add(article.url)
                state["posted"] = list(posted)[-300:]
                state["initializedSources"] = sorted(initialized)
                state["emptyRuns"] = dict(empty_runs)
                self.news_state.save(state)
                stats["posted"] += 1
            else:
                stats["publish_failed"] += 1

        state["posted"] = list(posted)[-300:]
        state["initializedSources"] = sorted(initialized)
        state["emptyRuns"] = dict(empty_runs)
        self.news_state.save(state)
        report.append(
            "- 결과: 게시 {posted} · 필터 탈락 {rejected} · 요약 실패 {summary_failed} · 게시 실패 {publish_failed}".format(
                posted=stats["posted"],
                rejected=stats["rejected"],
                summary_failed=stats["summary_failed"],
                publish_failed=stats["publish_failed"],
            )
        )
        await self._selection_log("**STN 뉴스룸 수집 요약**\n" + "\n".join(report))
        return {"stats": dict(stats), "report": report}

    async def run_repo(self) -> dict[str, Any]:
        github = GitHubSource(self.settings, self.session)
        state, healthy = self.repo_state.load()
        if (
            state.get("repo") != github.repo
            or state.get("branch") != github.branch
        ):
            state = repo_state_default(github.repo, github.branch)
            healthy = False
        commits, releases, tags = await github.snapshot()
        current_shas = [str(item.get("sha") or "") for item in commits if item.get("sha")]
        current_tags = [
            str(item.get("name") or "") for item in tags if item.get("name")
        ]
        release_tags = [
            str(item.get("tag_name") or "") for item in releases if item.get("tag_name")
        ]
        if not healthy or not state.get("initialized"):
            state.update(
                {
                    "repo": github.repo,
                    "branch": github.branch,
                    "initialized": True,
                    "seenCommits": current_shas[:100],
                    "seenTags": list(dict.fromkeys(release_tags + current_tags))[:100],
                }
            )
            self.repo_state.save(state)
            log.info(
                "Repository state seeded repo=%s commits=%s tags=%s; no backfill",
                github.repo,
                len(current_shas),
                len(state["seenTags"]),
            )
            return {"seeded": True, "posted": 0}

        seen_commits = {str(value) for value in state.get("seenCommits", [])}
        seen_tags = {str(value) for value in state.get("seenTags", [])}
        posted_count = 0
        limit = int(self.settings.config["limits"]["repoTotal"])
        tag_targets = {
            str(item.get("name")): str((item.get("commit") or {}).get("sha") or "")
            for item in tags
            if item.get("name")
        }

        new_releases = [
            release
            for release in releases
            if str(release.get("tag_name") or "") not in seen_tags
        ]
        for release in reversed(new_releases):
            if posted_count >= limit:
                break
            article = github.release_article(release)
            try:
                summary = await self.intelligence.summarize(article)
            except Exception as exc:
                log.error("Release summary failed tag=%s error=%s", article.meta.get("tag"), exc)
                continue
            message = format_release_message(article, summary, github.repo)
            if await self.publisher.post(self.settings.channels["github_repo"], message):
                tag = str(article.meta["tag"])
                seen_tags.add(tag)
                target = tag_targets.get(tag)
                if target:
                    seen_commits.add(target)
                posted_count += 1
                state["seenTags"] = list(seen_tags)[-100:]
                state["seenCommits"] = list(seen_commits)[-100:]
                self.repo_state.save(state)

        unseen_commits: list[str] = []
        for sha in current_shas:
            if sha in seen_commits:
                break
            unseen_commits.append(sha)
        for sha in reversed(unseen_commits[: max(0, limit - posted_count)]):
            article = await github.commit_article(sha)
            try:
                summary = await self.intelligence.summarize(article)
            except Exception as exc:
                log.error("Commit summary failed sha=%s error=%s", sha[:7], exc)
                continue
            message = format_commit_message(article, summary, github.repo)
            if await self.publisher.post(self.settings.channels["github_repo"], message):
                seen_commits.add(sha)
                posted_count += 1
                state["seenCommits"] = list(seen_commits)[-100:]
                state["seenTags"] = list(seen_tags)[-100:]
                self.repo_state.save(state)

        return {
            "seeded": False,
            "posted": posted_count,
            "newCommits": len(unseen_commits),
            "newReleases": len(new_releases),
        }
