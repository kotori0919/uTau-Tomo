from __future__ import annotations

import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class FakeStore:
    def __init__(self) -> None:
        self.messages: list[tuple[object, ...]] = []

    async def add_message(self, *args: object) -> None:
        self.messages.append(args)


class FakeClient:
    def __init__(self) -> None:
        self.responses = [
            '{"tool":"repeat"}',
            '{"tool":"repeat"}',
            "도구 결과를 바탕으로 정리한 최종 답변",
        ]
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **_kwargs: object) -> str:
        self.prompts.append(prompt)
        return self.responses[len(self.prompts) - 1]


class FakeRegistry:
    @staticmethod
    def names() -> set[str]:
        return {"repeat"}


class ToolLoopRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_research_reply_passes_exact_discord_reference_to_answer(self) -> None:
        channel = SimpleNamespace(id=10)
        content = "Hugging Face가 OpenRouter를 연동했습니다."
        quoted = SimpleNamespace(id=11, channel=channel, content=content, embeds=[])
        channel.fetch_message = AsyncMock(return_value=quoted)
        bot_user = SimpleNamespace(id=12)
        message = SimpleNamespace(
            guild=SimpleNamespace(id=13), channel=channel,
            author=SimpleNamespace(bot=False), mentions=[bot_user],
            content="<@12> 더 조사해보거라",
            reference=SimpleNamespace(type=bot.discord.MessageReferenceType.default,
                                      message_id=11, channel_id=10, resolved=None),
            reply=AsyncMock(), attachments=[],
        )
        store = SimpleNamespace(is_llm_enabled=AsyncMock(return_value=True))
        conversation = SimpleNamespace(answer=AsyncMock(return_value="조사 결과"))
        cog = bot.AI(SimpleNamespace(user=bot_user), store, conversation, None)
        with patch.object(bot, "DiscordStreamEditor", return_value=SimpleNamespace(
            update=AsyncMock(), finish=AsyncMock()
        )):
            await cog.on_message(message)
        channel.fetch_message.assert_awaited_once_with(11)
        self.assertEqual(conversation.answer.await_args.kwargs["reply_context"], content)
        self.assertEqual(conversation.answer.await_args.kwargs["content"], "더 조사해보거라")

        message.reference.channel_id = 99
        channel.fetch_message.reset_mock()
        self.assertEqual(await cog.replied_message_context(message), "")
        channel.fetch_message.assert_not_awaited()

    async def test_reply_research_treats_model_keywords_as_search_query(self) -> None:
        client = SimpleNamespace(generate=AsyncMock(side_effect=[
            '{"query":"Hugging Face OpenRouter integration"}', "연동 조사 결과",
        ]))
        service = bot.AIConversationService(
            SimpleNamespace(), FakeStore(), client,
            SimpleNamespace(names=lambda: {"web_search", "ask_user"}),
        )
        service.run_tool = AsyncMock(return_value=bot.ToolResult("URL: https://example.org/announcement"))
        service.build_prompt = AsyncMock(return_value="answer prompt")
        service.safe_compress_if_needed = AsyncMock()
        answer = await service.answer(
            guild=SimpleNamespace(id=1), channel_id=3,
            user=SimpleNamespace(id=2, display_name="tester"),
            content="더 조사해보거라", kind="direct",
            reply_context="Hugging Face와 OpenRouter가 연동됐습니다.",
        )
        self.assertEqual(answer, "연동 조사 결과")
        self.assertEqual(service.run_tool.await_args.args[1],
                         {"tool": "web_search", "query": "Hugging Face OpenRouter integration"})
        self.assertIn("https://example.org/announcement",
                      service.build_prompt.await_args.kwargs["tool_outputs"][0])

    async def test_latest_news_search_uses_focused_dated_feed_before_backup(self) -> None:
        question = "웹 검색 가능해? 수능 관련해서 최신 뉴스 몇 개만 가져와봐"
        client = bot.WebSearchClient(True, 3)
        article = bot.WebSearchResult("수능 예비소집", "https://example.org/news", "날짜: 2026-11-18")
        client._search_news_feed = AsyncMock(side_effect=[[article], []])
        with patch.object(client, "_search_sync", return_value=[article]) as backup:
            self.assertEqual(await client.search(bot.proactive_web_query(question)), [article])
            client._search_news_feed.assert_awaited_once_with("수능 when:2d")
            backup.assert_not_called()
            self.assertEqual(await client.search(question), [article])
            backup.assert_called_once_with(question)

    async def test_general_search_uses_news_feed_when_provider_fails(self) -> None:
        query = "Hugging Face OpenRouter integration"
        client = bot.WebSearchClient(True, 3)
        article = bot.WebSearchResult("Hugging Face OpenRouter migration", "https://example.org/guide", "")
        client._search_news_feed = AsyncMock(return_value=[article])
        with patch.object(client, "_search_sync", side_effect=RuntimeError("provider unavailable")):
            self.assertEqual(await client.search(query), [article])
        client._search_news_feed.assert_awaited_once_with(query)

    async def test_meme_lookup_searches_before_generating_an_answer(self) -> None:
        service = bot.AIConversationService(
            SimpleNamespace(), FakeStore(), SimpleNamespace(generate=AsyncMock(return_value="검색한 답변")),
            SimpleNamespace(names=lambda: {"web_search"}),
        )
        service.run_tool = AsyncMock(return_value=bot.ToolResult("URL: https://example.org/meme"))
        service.build_prompt = AsyncMock(return_value="prompt")
        service.safe_compress_if_needed = AsyncMock()
        answer = await service.answer(
            guild=SimpleNamespace(id=1), channel_id=3,
            user=SimpleNamespace(id=2, display_name="tester"),
            content="ram altman 밈에 대해 알려줘", kind="mention",
        )
        self.assertEqual(answer, "검색한 답변")
        self.assertEqual(service.run_tool.await_args.args[1],
                         {"tool": "web_search", "query": "ram altman meme"})
        self.assertIn("https://example.org/meme", service.build_prompt.await_args.kwargs["tool_outputs"][0])
        self.assertIsNone(bot.proactive_web_query("안녕!"))

    async def test_tool_limit_gets_one_tool_free_finalization_pass(self) -> None:
        service = bot.AIConversationService.__new__(bot.AIConversationService)
        service.bot = SimpleNamespace()
        service.store = FakeStore()
        service.client = FakeClient()
        service.tool_registry = FakeRegistry()

        async def build_prompt(_self: object, *_args: object, **kwargs: object) -> str:
            outputs = kwargs.get("tool_outputs", [])
            return "prompt\n" + "\n".join(outputs)

        async def run_tool(
            _self: object, _context: object, _call: object
        ) -> bot.ToolResult:
            return bot.ToolResult("반복 도구 결과", final=False)

        async def compress(_self: object, *_args: object) -> None:
            return None

        service.build_prompt = MethodType(build_prompt, service)
        service.run_tool = MethodType(run_tool, service)
        service.safe_compress_if_needed = MethodType(compress, service)

        guild = SimpleNamespace(id=1)
        user = SimpleNamespace(id=2, display_name="tester")
        with patch.object(bot, "LLM_TOOL_MAX_STEPS", 2):
            answer = await service.answer(
                guild=guild,
                channel_id=3,
                user=user,
                content="반복 호출 재현",
                kind="mention",
            )

        self.assertEqual(answer, "도구 결과를 바탕으로 정리한 최종 답변")
        self.assertEqual(len(service.client.prompts), 3)
        self.assertIn("도구를 더 호출하지 말고", service.client.prompts[-1])


if __name__ == "__main__":
    unittest.main()
