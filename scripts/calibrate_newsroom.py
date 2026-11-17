from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from newsroom_worker.config import Settings
from newsroom_worker.intelligence import IntelligenceClient
from newsroom_worker.models import Article


async def main() -> None:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    settings = Settings.from_env(root)
    rows = json.loads(
        (root / "tests/fixtures/newsroom_labels.json").read_text(encoding="utf-8")
    )
    results: list[tuple[str, bool, float, float, str]] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        client = IntelligenceClient(settings, session)
        for index, row in enumerate(rows):
            article = Article(
                source="samsung_semiconductor",
                url=f"https://example.invalid/{index}",
                title=row["title"],
                body=row["body"],
                section=row["section"],
            )
            decision = await client.classify(article)
            results.append(
                (
                    row["label"],
                    decision.keep,
                    decision.knowledge_score,
                    decision.promotion_score,
                    row["title"],
                )
            )
            print(
                f"{row['label']:9} keep={str(decision.keep):5} "
                f"k={decision.knowledge_score:.4f} p={decision.promotion_score:.4f} "
                f"{row['title']}"
            )
    knowledge = [row for row in results if row[0] == "knowledge"]
    promotion = [row for row in results if row[0] == "promotion"]
    recall = sum(row[1] for row in knowledge) / len(knowledge)
    false_positives = sum(row[1] for row in promotion)
    print(
        f"summary total={len(results)} promotion_false_positives={false_positives} "
        f"knowledge_recall={recall:.3f}"
    )
    if false_positives or recall < 0.9:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
