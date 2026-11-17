from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from newsroom_worker.config import Settings
from newsroom_worker.discord_client import format_commit_message
from newsroom_worker.intelligence import IntelligenceClient
from newsroom_worker.models import Article, canonicalize_url
from newsroom_worker.pipeline import NewsroomPipeline
from newsroom_worker.scheduler import Scheduler, seconds_until_news_run
from newsroom_worker.state import AtomicJsonState


class FakePublisher:
    def __init__(self, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.messages: list[tuple[str, str]] = []

    async def post(self, channel_id: str, content: str) -> bool:
        self.messages.append((channel_id, content))
        return self.succeeds


class FakeIntelligence:
    @staticmethod
    async def summarize(article: Article) -> str:
        return f"{article.title} 변경 내용을 정리했습니다."


class FakeGitHubSource:
    def __init__(self, settings: Settings, _session: object) -> None:
        self.repo = settings.github["repo"]
        self.branch = settings.github["branch"]

    async def snapshot(self):
        return ([{"sha": "new-sha"}, {"sha": "old-sha"}], [], [])

    async def commit_article(self, sha: str) -> Article:
        return Article(
            source="github_repo",
            url=f"https://github.com/example/repo/commit/{sha}",
            title="새 커밋",
            body="재생 흐름을 정리했습니다.",
            published_at="2026-11-18T00:00:00+00:00",
            meta={"sha": sha, "files": 2, "additions": 10, "deletions": 3},
        )

    def release_article(self, _release: object) -> Article:
        raise AssertionError("no releases in this fixture")


class NewsroomUnitTests(unittest.TestCase):
    def test_canonical_url_removes_tracking_and_threads_media(self) -> None:
        self.assertEqual(
            canonicalize_url("https://EXAMPLE.com/post/?utm_source=x&type=main&id=3#top"),
            "https://example.com/post?id=3",
        )
        self.assertEqual(
            canonicalize_url("https://www.threads.com/@x/post/abc/media?hl=ko"),
            "https://www.threads.com/@x/post/abc",
        )

    def test_atomic_state_recovers_from_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            store = AtomicJsonState(path, {"posted": []})
            data, healthy = store.load()
            self.assertFalse(healthy)
            self.assertEqual(data, {"posted": []})
            store.save({"posted": ["https://example.com"]})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["posted"],
                ["https://example.com"],
            )

    def test_promotion_title_rule_rejects_without_model_call(self) -> None:
        settings = Settings.from_env(Path.cwd())
        client = IntelligenceClient(settings, SimpleNamespace())
        article = Article(
            source="samsung_semiconductor",
            url="https://example.com/award",
            title="삼성전자, CES 2027 혁신상 수상",
            body="수상 목록을 발표했습니다.",
        )
        decision = self._run(client.classify(article))
        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "promotion title rule")

    def test_commit_message_matches_handoff_format(self) -> None:
        article = Article(
            source="github_repo",
            url="https://github.com/example/repo/commit/abcdef123",
            title="Update",
            body="body",
            published_at="2026-11-17T12:00:00+09:00",
            meta={"sha": "abcdef123", "files": 4, "additions": 120, "deletions": 38},
        )
        message = format_commit_message(article, "재생 흐름을 정리했습니다.", "example/repo")
        self.assertIn("**dimigo-HD-Music-Bot 업데이트 · abcdef1**", message)
        self.assertIn("변경: 파일 4개 (+120 / −38)", message)
        self.assertIn("출처: GitHub example/repo (2026.11.17)", message)

    def test_news_schedule_targets_0830_kst(self) -> None:
        now = datetime(2026, 11, 18, 8, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        with patch("newsroom_worker.scheduler.random.uniform", return_value=0):
            self.assertEqual(seconds_until_news_run(now), 30 * 60)

    @staticmethod
    def _run(awaitable):
        import asyncio

        return asyncio.run(awaitable)


class RepositoryIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_runs_news_without_repository_updates(self) -> None:
        scheduler = Scheduler(SimpleNamespace())
        calls = []

        async def news_loop() -> None:
            calls.append("news")

        scheduler._news_loop = news_loop
        await scheduler.run()
        self.assertEqual(calls, ["news"])

    async def test_new_commit_posts_once_and_second_run_posts_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = Settings.from_env(Path.cwd())
            settings = replace(
                base,
                state_path=root / "state.json",
                repo_state_path=root / "repo-state.json",
                dry_run=True,
            )
            settings.repo_state_path.write_text(
                json.dumps(
                    {
                        "repo": settings.github["repo"],
                        "branch": settings.github["branch"],
                        "initialized": True,
                        "seenCommits": ["old-sha"],
                        "seenTags": [],
                    }
                ),
                encoding="utf-8",
            )
            pipeline = NewsroomPipeline(settings, SimpleNamespace())
            publisher = FakePublisher()
            pipeline.publisher = publisher
            pipeline.intelligence = FakeIntelligence()

            with patch("newsroom_worker.pipeline.GitHubSource", FakeGitHubSource):
                first = await pipeline.run_repo()
                second = await pipeline.run_repo()

            self.assertEqual(first["posted"], 1)
            self.assertEqual(second["posted"], 0)
            self.assertEqual(len(publisher.messages), 1)

    async def test_failed_publish_is_retried_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = Settings.from_env(Path.cwd())
            settings = replace(
                base,
                state_path=root / "state.json",
                repo_state_path=root / "repo-state.json",
                dry_run=True,
            )
            settings.repo_state_path.write_text(
                json.dumps(
                    {
                        "repo": settings.github["repo"],
                        "branch": settings.github["branch"],
                        "initialized": True,
                        "seenCommits": ["old-sha"],
                        "seenTags": [],
                    }
                ),
                encoding="utf-8",
            )
            pipeline = NewsroomPipeline(settings, SimpleNamespace())
            publisher = FakePublisher(succeeds=False)
            pipeline.publisher = publisher
            pipeline.intelligence = FakeIntelligence()
            with patch("newsroom_worker.pipeline.GitHubSource", FakeGitHubSource):
                first = await pipeline.run_repo()
                publisher.succeeds = True
                second = await pipeline.run_repo()
            self.assertEqual(first["posted"], 0)
            self.assertEqual(second["posted"], 1)
            self.assertEqual(len(publisher.messages), 2)


if __name__ == "__main__":
    unittest.main()
