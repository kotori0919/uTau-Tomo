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
    async def test_delivery_routes_by_content_retries_failure_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings.from_env(Path.cwd()),
                               state_path=Path(directory) / "state.json",
                               repo_state_path=Path(directory) / "repo.json")
            pipeline = NewsroomPipeline(settings, None)
            pipeline.news_state.save({"initializedSources": ["feed_test"],
                                      "editorialModel": settings.embedding_model,
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
