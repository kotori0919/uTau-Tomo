from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from .config import Settings
from .models import Article
from .pipeline import NewsroomPipeline
from .scheduler import Scheduler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STN newsroom worker")
    parser.add_argument("command", choices=("run", "once", "check"))
    parser.add_argument(
        "target", nargs="?", choices=("all", "news"), default="all"
    )
    return parser.parse_args()


async def run_command(args: argparse.Namespace, settings: Settings) -> None:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        pipeline = NewsroomPipeline(settings, session)
        if args.command == "check":
            errors = settings.validate()
            if errors:
                raise RuntimeError("; ".join(errors))
            vectors = await pipeline.intelligence.embed_many(["연결 확인"])
            summary = await pipeline.intelligence.summarize(
                Article(
                    source="samsung_semiconductor",
                    url="https://example.invalid/preflight",
                    title="HBM 연결 확인",
                    body="여러 디램 다이를 수직 적층해 메모리 대역폭을 높이는 원리를 설명한다.",
                    section="반도챗",
                )
            )
            print(
                f"configuration=ok embedding_model={settings.embedding_model} "
                f"dimensions={len(vectors[0])} summary_model={settings.summary_model} "
                f"summary_chars={len(summary)}"
            )
            permission_errors: list[str] = []
            for key, channel_id in settings.channels.items():
                status, name = await pipeline.publisher.channel_status(channel_id)
                permissions = await pipeline.publisher.effective_channel_permissions(
                    channel_id
                )
                print(
                    f"channel={key} id={channel_id} status={status} name={name} "
                    f"permissions={permissions}"
                )
                missing = [
                    name
                    for name in ("view", "send", "embed", "history")
                    if not permissions[name]
                ]
                if missing:
                    permission_errors.append(f"{key}: missing {', '.join(missing)}")
                if permissions["manage_channels"]:
                    permission_errors.append(f"{key}: Manage Channels must not be granted")
            if permission_errors:
                raise RuntimeError("; ".join(permission_errors))
            return
        if args.command == "run":
            errors = settings.validate()
            if errors:
                raise RuntimeError("; ".join(errors))
            await Scheduler(pipeline).run()
            return
        if args.target in {"all", "news"}:
            print(await pipeline.run_news())


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    settings = Settings.from_env(root)
    try:
        asyncio.run(run_command(args, settings))
    except RuntimeError as exc:
        logging.getLogger(__name__).error("%s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
