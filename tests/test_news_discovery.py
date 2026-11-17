import unittest
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from newsroom_worker.config import Settings
from newsroom_worker.feeds import parse_feed
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
