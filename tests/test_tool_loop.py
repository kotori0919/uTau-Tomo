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
