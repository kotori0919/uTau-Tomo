import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from newsroom_worker.editorial import NewsEditor, is_fresh
from newsroom_worker.models import Article


class EditorialTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_event_rejected_but_material_update_allowed(self):
        client = SimpleNamespace(embed_many=AsyncMock(return_value=[[1., 0.]]),
                                 _stream_summary=AsyncMock(return_value='{"useful":true,"new_facts":false,"reason":"reprint"}'))
        editor = NewsEditor(client)
        history = [{"text": "CVE fixed in version 2", "vector": [1., 0.]}]
        article = Article("other_publisher", "https://example.com/a", "CVE fix", "The vendor released version 2 to fix a remote code execution vulnerability.")
        keep, _, _ = await editor.review(article, history)
        self.assertFalse(keep)
        self.assertIn("CVE fixed in version 2", client._stream_summary.call_args.args[0][1]["content"])
        client._stream_summary.return_value = '{"useful":true,"new_facts":true,"reason":"new affected version"}'
        keep, _, _ = await editor.review(article, history)
        self.assertTrue(keep)

    async def test_string_true_does_not_pass_review(self):
        client = SimpleNamespace(embed_many=AsyncMock(return_value=[[1., 0.]]),
                                 _stream_summary=AsyncMock(return_value='{"useful":"true","new_facts":true}'))
        article = Article("x", "https://example.com", "Update", "The vendor released version 2 to fix a remote code execution vulnerability.")
        keep, _, _ = await NewsEditor(client).review(article, [])
        self.assertFalse(keep)
        client._stream_summary.assert_awaited_once()

    async def test_repeated_headline_without_article_body_is_rejected(self):
        title = "AI 커닝 파장에 공무원 시험 관리도 비상"
        client = SimpleNamespace(embed_many=AsyncMock())
        keep, _, _ = await NewsEditor(client).review(
            Article("aggregator", "https://example.com", title, title + " 강원일보"), []
        )
        self.assertFalse(keep)
        client.embed_many.assert_not_awaited()

    def test_missing_future_and_stale_dates_are_rejected(self):
        now = datetime.now(timezone.utc)
        article = Article("x", "https://example.com", "Title", "Body")
        for date in (None, "invalid", (now - timedelta(hours=25)).isoformat(), (now + timedelta(hours=1)).isoformat()):
            article.published_at = date
            self.assertFalse(is_fresh(article, now, 24))
        article.published_at = (now - timedelta(minutes=10)).isoformat()
        self.assertTrue(is_fresh(article, now, 24))
