import unittest
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from newsroom_worker.config import Settings
from newsroom_worker.feeds import collect_feeds, parse_feed
from newsroom_worker.intelligence import IntelligenceClient
from newsroom_worker.models import Article, SourceBatch
from newsroom_worker.pipeline import NewsroomPipeline


class FeedTests(unittest.TestCase):
    def test_rss_extracts_date_body_and_publisher(self):
        payload = b'''<rss><channel><item><title>Security patch</title>
        <link>https://example.com/a?utm_source=rss</link>
        <pubDate>Wed, 18 Nov 2026 01:00:00 GMT</pubDate>
        <description>&lt;p&gt;Fixes a vulnerability&lt;/p&gt;</description>
        </item><item><title>Undated item</title><link>https://example.com/b</link></item>
        </channel></rss>'''
        rows = parse_feed(payload, "feed_test", "Publisher")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].url, "https://example.com/a")
        self.assertEqual(rows[0].body, "Fixes a vulnerability")
        self.assertEqual(rows[0].meta["source_label"], "Publisher")
        self.assertEqual(rows[0].published_at, "2026-11-18T01:00:00+00:00")

    def test_atom_extracts_alternate_link(self):
        rows = parse_feed(b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Research paper</title><link href="https://example.com/paper"/>
        <updated>2026-11-18T00:00:00Z</updated><summary>Experiment results</summary>
        </entry></feed>''', "feed_papers", "Papers")
        self.assertEqual(rows[0].url, "https://example.com/paper")


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_collector_distinguishes_empty_entries_from_invalid_entries(self):
        def response(body):
            async def chunks(_size):
                yield body
            return AsyncMock(__aenter__=AsyncMock(return_value=SimpleNamespace(
                raise_for_status=lambda: None, content=SimpleNamespace(iter_chunked=chunks)
            )))

        feeds = [
            {"id": key, "label": key, "url": f"https://example.org/{key}"}
            for key in ("weekend", "invalid_item", "html", "empty_atom")
        ]
        responses = [
            response(b"<rss><channel><title>cs.AI updates on arXiv.org</title></channel></rss>"),
            response(b"<rss><channel><item><title>Invalid paper</title></item></channel></rss>"),
            response(b"<html><body>Not a feed</body></html>"),
            response(b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Empty</title></feed>'),
        ]
        with patch("aiohttp.ClientSession.get", side_effect=responses):
            import aiohttp

            async with aiohttp.ClientSession() as session:
                batches = await collect_feeds(session, feeds)
        self.assertEqual([batch.status for batch in batches],
                         ["no_updates", "parse_empty", "error", "no_updates"])

    async def test_empty_feed_clears_pending_parser_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Settings.from_env(Path.cwd())
            settings = replace(base, config={**base.config, "quietHoursKst": None},
                               state_path=Path(directory) / "state.json")
            pipeline = NewsroomPipeline(settings, None)
            now = time.time()
            source = "feed_arxiv_ai"
            pipeline.news_state.save({"editorialModel": settings.embedding_model,
                                      "lastEditorialImport": now, "lastBrowserPoll": now,
                                      "lastThreadsPoll": now, "emptyRuns": {source: 2},
                                      "lastLogDate": datetime.now(timezone.utc).date().isoformat(),
                                      "pendingSourceAlerts": [source]})
            pipeline.publisher.post = AsyncMock()
            with patch("newsroom_worker.pipeline.collect_feeds", AsyncMock(
                return_value=[SourceBatch(source, [], "no_updates")]
            )):
                result = await pipeline.run_news()
            self.assertIn(f"- {source}: 신규 소식 없음", result["report"])
            state, _ = pipeline.news_state.load()
            self.assertEqual(state["emptyRuns"][source], 0)
            self.assertEqual(state["pendingSourceAlerts"], [])
            pipeline.publisher.post.assert_not_awaited()

    async def test_overnight_collection_survives_restart_and_delivers_at_seven_kst(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings.from_env(Path.cwd()), dry_run=False,
                               state_path=Path(directory) / "state.json",
                               repo_state_path=Path(directory) / "repo.json")
            night = datetime.fromisoformat("2026-11-18T06:59:00+09:00")
            morning = datetime.fromisoformat("2026-11-18T07:00:00+09:00")
            self.assertTrue(settings.publishing_paused(datetime.fromisoformat("2026-11-17T23:00:00+09:00")))
            self.assertFalse(settings.publishing_paused(datetime.fromisoformat("2026-11-17T22:59:59+09:00")))
            threads = Article("threads_choi_openai", "https://www.threads.com/@choi.openai/post/night",
                              "밤에 나온 글", "원문", night.isoformat())
            news = Article("feed_test", "https://example.org/overnight", "Night news", "Body",
                           night.isoformat())
            batches = [[SourceBatch(news.source, [news]), SourceBatch("empty_feed", [], "parse_empty")], []]
            pipeline = NewsroomPipeline(settings, None)
            pipeline.news_state.save({"initializedSources": [news.source], "lastBrowserPoll": night.timestamp(),
                                      "editorialModel": settings.embedding_model,
                                      "lastEditorialImport": night.timestamp(), "emptyRuns": {"empty_feed": 1}})
            pipeline.publisher.post = AsyncMock()
            pipeline.editor.review = AsyncMock()
            with patch("newsroom_worker.pipeline.datetime", wraps=datetime) as clock, patch(
                "newsroom_worker.pipeline.collect_feeds", AsyncMock(side_effect=batches)
            ), patch("newsroom_worker.pipeline.BrowserSources") as browser:
                browser.return_value.__aenter__.return_value.collect_threads = AsyncMock(
                    return_value=SourceBatch(threads.source, [threads])
                )
                clock.now.return_value = night.astimezone(timezone.utc)
                held = await pipeline.run_news()
                self.assertEqual(held["stats"]["quiet_hours_pending"], 2)
                pipeline.publisher.post.assert_not_awaited()
                pipeline.editor.review.assert_not_awaited()
                self.assertNotIn("lastLogDate", pipeline.news_state.load()[0])

                clock.now.return_value = morning.astimezone(timezone.utc)
                restarted = NewsroomPipeline(settings, None)
                restarted.publisher.post = AsyncMock(return_value=True)
                restarted.editor.review = AsyncMock(return_value=(True, "new facts", [1., 0.]))
                restarted.intelligence.route = AsyncMock(return_value=("technology", 0.9))
                restarted.intelligence.summarize = AsyncMock(return_value="새 기능이 공개됐습니다.")
                result = await restarted.run_news()
            self.assertEqual(result["stats"]["threads_posted"], 1)
            self.assertEqual(result["stats"]["posted"], 1)
            sent = [call.args[1] for call in restarted.publisher.post.await_args_list]
            self.assertTrue(any(threads.url in msg for msg in sent))
            self.assertTrue(any(news.url in msg for msg in sent))
            self.assertTrue(any("셀렉터 점검 필요" in msg for msg in sent))
            self.assertTrue(any("STN 뉴스룸 수집 요약" in msg for msg in sent))
            self.assertEqual(restarted.news_state.load()[0]["pendingNews"], [])

    async def test_daily_report_retries_failed_delivery_and_records_success(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings.from_env(Path.cwd()), dry_run=False,
                               state_path=Path(directory) / "state.json",
                               repo_state_path=Path(directory) / "repo.json")
            pipeline = NewsroomPipeline(settings, None)
            now = time.time()
            pipeline.news_state.save({"editorialModel": settings.embedding_model,
                                      "lastEditorialImport": now, "lastBrowserPoll": now,
                                      "lastThreadsPoll": now})
            pipeline.publisher.post = AsyncMock(side_effect=[False, OSError("connection lost"), True])
            with patch("newsroom_worker.pipeline.collect_feeds", AsyncMock(return_value=[])):
                for _ in range(2):
                    await pipeline.run_news()
                    self.assertNotIn("lastLogDate", pipeline.news_state.load()[0])
                await pipeline.run_news()
                self.assertEqual(pipeline.news_state.load()[0]["lastLogDate"],
                                 datetime.now(timezone.utc).date().isoformat())
                await pipeline.run_news()
            self.assertEqual(pipeline.publisher.post.await_count, 3)
            self.assertEqual(pipeline.publisher.post.await_args.args[0], settings.channels["selection_log"])

    async def test_priority_threads_posts_without_models_or_news_quota_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings.from_env(Path.cwd()),
                               state_path=Path(directory) / "state.json",
                               repo_state_path=Path(directory) / "repo.json")
            pipeline = NewsroomPipeline(settings, None)
            article = Article("threads_choi_openai", "https://www.threads.com/@choi.openai/post/test",
                              "짧은 글", "기사 링크", datetime.now(timezone.utc).isoformat())
            old = replace(article, url=article.url + "-old", published_at="2020-01-01T00:00:00Z")
            undated = replace(article, url=article.url + "-undated", published_at=None)
            now = time.time()
            pipeline.news_state.save({"editorialModel": settings.embedding_model,
                                      "lastEditorialImport": now, "lastBrowserPoll": now,
                                      "dailyDate": datetime.now(timezone.utc).date().isoformat(),
                                      "dailyPosted": 99, "reviewedUrls": [article.url]})
            pipeline.editor.review = AsyncMock()
            pipeline.intelligence.route = AsyncMock()
            pipeline.intelligence.summarize = AsyncMock()
            pipeline._selection_log = AsyncMock()
            pipeline.publisher.post = AsyncMock(side_effect=[False, True])
            with patch("newsroom_worker.pipeline.collect_feeds", AsyncMock(return_value=[])), patch(
                "newsroom_worker.pipeline.BrowserSources"
            ) as browser:
                variant = replace(article, url="http://threads.net/@CHOI.OPENAI/post/test/media?xmt=tracking")
                self.assertEqual(variant.url, article.url)
                browser.return_value.__aenter__.return_value.collect_threads = AsyncMock(
                    side_effect=[SourceBatch(article.source, [candidate, old, undated])
                                 for candidate in (article, article, variant)]
                )
                for _ in range(3):
                    state, _ = pipeline.news_state.load()
                    pipeline.news_state.save({**state, "lastThreadsPoll": 0})
                    await pipeline.run_news()
            self.assertEqual(pipeline.publisher.post.await_count, 2)
            self.assertEqual(pipeline.publisher.post.call_args.args[0], settings.channels[article.source])
            self.assertIn(article.url, pipeline.publisher.post.call_args.args[1])
            pipeline.editor.review.assert_not_awaited()
            pipeline.intelligence.route.assert_not_awaited()
            pipeline.intelligence.summarize.assert_not_awaited()

    async def test_delivery_routes_by_content_retries_failure_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings.from_env(Path.cwd()),
                               state_path=Path(directory) / "state.json",
                               repo_state_path=Path(directory) / "repo.json")
            pipeline = NewsroomPipeline(settings, None)
            pipeline.news_state.save({"initializedSources": ["feed_test"],
                                      "editorialModel": settings.embedding_model,
                                      "lastThreadsPoll": time.time(),
                                      "lastEditorialImport": time.time(),
                                      "posted": [], "lastBrowserPoll": time.time()})
            pipeline.editor.review = AsyncMock(return_value=(True, "New patch", [1., 0.]))
            article = Article("feed_test", "https://example.com/cve", "Vulnerability",
                              "New security patch", datetime.now(timezone.utc).isoformat())
            pipeline.intelligence.route = AsyncMock(return_value=("security", 0.9))
            pipeline.intelligence.summarize = AsyncMock(return_value="취약점 보안 패치를 공개했습니다.")
            pipeline.publisher.post = AsyncMock(return_value=False)
            pipeline._selection_log = AsyncMock()
            with patch("newsroom_worker.pipeline.collect_feeds", AsyncMock(return_value=[SourceBatch("feed_test", [article])])):
                first = await pipeline.run_news()
                self.assertEqual(first["stats"].get("posted", 0), 0)
                pipeline.publisher.post = AsyncMock(return_value=True)
                second = await pipeline.run_news()
                self.assertEqual(second["stats"]["posted"], 1)
                self.assertEqual(pipeline.publisher.post.call_args.args[0], settings.channels["security"])
                third = await pipeline.run_news()
                self.assertEqual(third["stats"].get("posted", 0), 0)
                self.assertEqual(pipeline.publisher.post.await_count, 1)

    async def test_routing_uses_content_not_source_and_caches_anchors(self):
        settings = Settings.from_env(Path.cwd())
        settings = replace(settings, config={**settings.config, "routingAnchors": {
            "technology": ["Software"], "security": ["Vulnerabilities"]}})
        client = IntelligenceClient(settings, None)
        client.embed_many = AsyncMock(side_effect=[[[1., 0.], [0., 1.]], [[0., 1.]], [[1., 0.]]])
        article = Article("same_source", "https://example.com/1", "CVE", "Exploit")
        channel, score = await client.route(article)
        self.assertEqual(channel, "security")
        self.assertAlmostEqual(score, 1.)
        article = Article("same_source", "https://example.com/2", "Python", "Language update")
        channel, _ = await client.route(article)
        self.assertEqual(channel, "technology")
        self.assertEqual(client.embed_many.await_count, 3)
