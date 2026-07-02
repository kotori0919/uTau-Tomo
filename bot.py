from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import sqlite3
import subprocess
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

try:
    from discord.ext import voice_recv
except ImportError:
    voice_recv = None


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
OPUS_LIBRARY_PATH = os.getenv("OPUS_LIBRARY_PATH")
PORT = os.getenv("PORT")
HEALTH_HOST = os.getenv("HEALTH_HOST", "0.0.0.0")
SYNC_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.0.42:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4-E4B-Unhold:latest")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))

DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "data"))
MEMORY_DB_PATH = Path(os.getenv("MEMORY_DB_PATH", DATA_DIR / "memory.sqlite3"))
USER_COMPRESS_AFTER = int(os.getenv("USER_COMPRESS_AFTER", "40"))
USER_RECENT_KEEP = int(os.getenv("USER_RECENT_KEEP", "16"))
GUILD_COMPRESS_AFTER = int(os.getenv("GUILD_COMPRESS_AFTER", "160"))
GUILD_RECENT_KEEP = int(os.getenv("GUILD_RECENT_KEEP", "40"))

SUPERTONIC_DEFAULT_VOICE = os.getenv("SUPERTONIC_DEFAULT_VOICE", "M4")
SUPERTONIC_DEFAULT_LANG = os.getenv("SUPERTONIC_DEFAULT_LANG", "ko")
SUPERTONIC_MAX_CHARS = int(os.getenv("SUPERTONIC_MAX_CHARS", "700"))
SUPERTONIC_VOICE_BUILDER_CMD = os.getenv("SUPERTONIC_VOICE_BUILDER_CMD")

VOICE_SILENCE_SECONDS = float(os.getenv("VOICE_SILENCE_SECONDS", "1.2"))
VOICE_MIN_SECONDS = float(os.getenv("VOICE_MIN_SECONDS", "0.65"))
VOICE_SAMPLE_RATE = 48000
VOICE_CHANNELS = 2
VOICE_SAMPLE_WIDTH = 2
VOICE_MIN_BYTES = int(VOICE_SAMPLE_RATE * VOICE_CHANNELS * VOICE_SAMPLE_WIDTH * VOICE_MIN_SECONDS)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}

if OPUS_LIBRARY_PATH:
    discord.opus.load_opus(OPUS_LIBRARY_PATH)

DATA_DIR.mkdir(parents=True, exist_ok=True)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class MusicError(Exception):
    """Error message that is safe to show in Discord."""


class AIError(Exception):
    """Error message that is safe to show in Discord."""


@dataclass
class Track:
    query: str
    title: str
    stream_url: str
    webpage_url: str | None = None


@dataclass
class GuildMusicState:
    queue: deque[Track] = field(default_factory=deque)
    current: Track | None = None
    volume: float = 1.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class VoiceConversationState:
    text_channel_id: int
    sink: "ConversationAudioSink"
    monitor_task: asyncio.Task
    always_reply: bool = True
    processing_users: set[int] = field(default_factory=set)


async def send_interaction(
    interaction: discord.Interaction, content: str, *, ephemeral: bool = False
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)


async def send_long_message(destination: discord.abc.Messageable, content: str) -> None:
    chunks = []
    text = content.strip() or "(빈 응답)"
    while len(text) > 1900:
        split_at = text.rfind("\n", 0, 1900)
        if split_at < 500:
            split_at = 1900
        chunks.append(text[:split_at])
        text = text[split_at:].strip()
    chunks.append(text)

    for chunk in chunks:
        await destination.send(chunk)


def clean_model_response(text: str) -> str:
    text = re.sub(r"<\|channel\>thought.*?<channel\|>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|channel\>final\n?", "", text)
    text = text.replace("<channel|>", "")
    return text.strip()


class MemoryStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(db_path)
        self.connection.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self._init_db()

    def _init_db(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_memory (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS guild_memory (
                guild_id INTEGER PRIMARY KEY,
                summary TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS voice_settings (
                guild_id INTEGER PRIMARY KEY,
                voice_name TEXT,
                voice_style_path TEXT,
                lang TEXT,
                updated_at REAL NOT NULL
            );
            """
        )
        self.connection.commit()

    async def add_message(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        author_name: str,
        role: str,
        kind: str,
        content: str,
    ) -> None:
        content = content.strip()
        if not content:
            return

        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO messages
                (guild_id, channel_id, user_id, author_name, role, kind, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    user_id,
                    author_name[:120],
                    role,
                    kind,
                    content[:4000],
                    time.time(),
                ),
            )
            self.connection.commit()

    async def fetch_user_summary(self, guild_id: int, user_id: int) -> str:
        async with self.lock:
            row = self.connection.execute(
                "SELECT summary FROM user_memory WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return row["summary"] if row else ""

    async def fetch_guild_summary(self, guild_id: int) -> str:
        async with self.lock:
            row = self.connection.execute(
                "SELECT summary FROM guild_memory WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            return row["summary"] if row else ""

    async def fetch_recent_user(self, guild_id: int, user_id: int, limit: int) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.connection.execute(
                """
                SELECT * FROM messages
                WHERE guild_id = ? AND user_id = ? AND kind IN ('direct', 'voice')
                ORDER BY id DESC
                LIMIT ?
                """,
                (guild_id, user_id, limit),
            ).fetchall()
            return list(reversed(rows))

    async def fetch_recent_guild(self, guild_id: int, limit: int) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.connection.execute(
                """
                SELECT * FROM messages
                WHERE guild_id = ? AND role = 'user'
                ORDER BY id DESC
                LIMIT ?
                """,
                (guild_id, limit),
            ).fetchall()
            return list(reversed(rows))

    async def user_rows_for_compression(
        self, guild_id: int, user_id: int
    ) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.connection.execute(
                """
                SELECT * FROM messages
                WHERE guild_id = ? AND user_id = ? AND kind IN ('direct', 'voice')
                ORDER BY id ASC
                """,
                (guild_id, user_id),
            ).fetchall()
            if len(rows) <= USER_COMPRESS_AFTER:
                return []
            return rows[: max(0, len(rows) - USER_RECENT_KEEP)]

    async def guild_rows_for_compression(self, guild_id: int) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.connection.execute(
                """
                SELECT * FROM messages
                WHERE guild_id = ? AND role = 'user'
                ORDER BY id ASC
                """,
                (guild_id,),
            ).fetchall()
            if len(rows) <= GUILD_COMPRESS_AFTER:
                return []
            return rows[: max(0, len(rows) - GUILD_RECENT_KEEP)]

    async def update_user_summary(
        self, guild_id: int, user_id: int, summary: str, compressed_ids: list[int]
    ) -> None:
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO user_memory (guild_id, user_id, summary, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    summary = excluded.summary,
                    updated_at = excluded.updated_at
                """,
                (guild_id, user_id, summary[:6000], time.time()),
            )
            if compressed_ids:
                placeholders = ",".join("?" for _ in compressed_ids)
                self.connection.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})", compressed_ids
                )
            self.connection.commit()

    async def update_guild_summary(
        self, guild_id: int, summary: str, compressed_ids: list[int]
    ) -> None:
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO guild_memory (guild_id, summary, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    summary = excluded.summary,
                    updated_at = excluded.updated_at
                """,
                (guild_id, summary[:6000], time.time()),
            )
            if compressed_ids:
                placeholders = ",".join("?" for _ in compressed_ids)
                self.connection.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})", compressed_ids
                )
            self.connection.commit()

    async def reset_user(self, guild_id: int, user_id: int) -> None:
        async with self.lock:
            self.connection.execute(
                "DELETE FROM user_memory WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            self.connection.execute(
                "DELETE FROM messages WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            self.connection.commit()

    async def reset_guild(self, guild_id: int) -> None:
        async with self.lock:
            self.connection.execute("DELETE FROM guild_memory WHERE guild_id = ?", (guild_id,))
            self.connection.execute("DELETE FROM messages WHERE guild_id = ?", (guild_id,))
            self.connection.commit()

    async def get_voice_settings(self, guild_id: int) -> dict[str, str | None]:
        async with self.lock:
            row = self.connection.execute(
                "SELECT voice_name, voice_style_path, lang FROM voice_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if not row:
                return {
                    "voice_name": SUPERTONIC_DEFAULT_VOICE,
                    "voice_style_path": None,
                    "lang": SUPERTONIC_DEFAULT_LANG,
                }
            return {
                "voice_name": row["voice_name"] or SUPERTONIC_DEFAULT_VOICE,
                "voice_style_path": row["voice_style_path"],
                "lang": row["lang"] or SUPERTONIC_DEFAULT_LANG,
            }

    async def set_voice_settings(
        self,
        guild_id: int,
        *,
        voice_name: str | None = None,
        voice_style_path: str | None = None,
        lang: str | None = None,
    ) -> None:
        current = await self.get_voice_settings(guild_id)
        voice_name = voice_name if voice_name is not None else current["voice_name"]
        voice_style_path = (
            voice_style_path if voice_style_path is not None else current["voice_style_path"]
        )
        lang = lang if lang is not None else current["lang"]

        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO voice_settings (guild_id, voice_name, voice_style_path, lang, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    voice_name = excluded.voice_name,
                    voice_style_path = excluded.voice_style_path,
                    lang = excluded.lang,
                    updated_at = excluded.updated_at
                """,
                (guild_id, voice_name, voice_style_path, lang, time.time()),
            )
            self.connection.commit()


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: int):
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    async def generate(self, prompt: str, *, media_paths: list[Path] | None = None) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": 64,
            },
        }
        if media_paths:
            payload["images"] = [
                base64.b64encode(path.read_bytes()).decode("ascii") for path in media_paths
            ]

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.base_url}/api/generate", json=payload) as response:
                    if response.status >= 400:
                        body = await response.text()
                        raise AIError(f"Ollama 오류 {response.status}: {body[:300]}")
                    data = await response.json()
        except asyncio.TimeoutError as exc:
            raise AIError("Ollama 응답 시간이 초과됐어요.") from exc
        except aiohttp.ClientError as exc:
            raise AIError(f"Ollama 서버에 연결하지 못했어요: {exc}") from exc

        answer = data.get("response", "")
        if not isinstance(answer, str) or not answer.strip():
            raise AIError("Ollama가 빈 응답을 반환했어요.")
        return clean_model_response(answer)


class AIConversationService:
    def __init__(self, store: MemoryStore, client: OllamaClient):
        self.store = store
        self.client = client
        self.compress_lock = asyncio.Lock()

    async def answer(
        self,
        *,
        guild: discord.Guild,
        channel_id: int,
        user: discord.abc.User,
        content: str,
        kind: str,
        media_path: Path | None = None,
        always_reply: bool = True,
    ) -> str | None:
        await self.store.add_message(
            guild.id, channel_id, user.id, user.display_name, "user", kind, content
        )
        prompt = await self.build_prompt(
            guild.id, user, content, kind, always_reply, media_path=media_path
        )
        answer = await self.client.generate(
            prompt, media_paths=[media_path] if media_path is not None else None
        )
        if answer.strip().upper() == "NO_REPLY":
            return None

        await self.store.add_message(
            guild.id, channel_id, user.id, "assistant", "assistant", kind, answer
        )
        asyncio.create_task(self.safe_compress_if_needed(guild.id, user.id))
        return answer

    async def build_prompt(
        self,
        guild_id: int,
        user: discord.abc.User,
        content: str,
        kind: str,
        always_reply: bool,
        media_path: Path | None = None,
    ) -> str:
        user_summary, guild_summary, recent_user, recent_guild = await asyncio.gather(
            self.store.fetch_user_summary(guild_id, user.id),
            self.store.fetch_guild_summary(guild_id),
            self.store.fetch_recent_user(guild_id, user.id, 18),
            self.store.fetch_recent_guild(guild_id, 24),
        )

        user_history = "\n".join(
            f"- {row['author_name']}({row['role']}): {row['content']}" for row in recent_user
        )
        guild_history = "\n".join(
            f"- {row['author_name']}: {row['content']}" for row in recent_guild
        )

        voice_instruction = ""
        if kind == "voice" and not always_reply:
            voice_instruction = (
                "\n음성 입력이 봇에게 하는 말이 아니거나 배경 대화로 보이면 정확히 NO_REPLY만 출력하세요."
            )

        current_input = content
        if kind == "voice":
            current_input = "첨부된 WAV 오디오를 먼저 듣고 그 발화에 답하세요."
        elif media_path is not None and media_path.suffix.lower() in AUDIO_SUFFIXES:
            current_input = f"{content}\n\n첨부된 오디오를 먼저 듣고, 텍스트 요청과 함께 답하세요."
        elif media_path is not None and media_path.suffix.lower() in IMAGE_SUFFIXES:
            current_input = f"{content}\n\n첨부된 이미지를 보고, 텍스트 요청과 함께 답하세요."

        return f"""
당신은 Discord 서버의 한국어 AI 대화 봇입니다.
사용자별 장기 memory, 최근 대화, 서버의 공유 문맥을 참고해 자연스럽고 짧게 답하세요.
서버 공유 문맥은 참고용이며, 다른 사용자의 사적인 정보를 단정하거나 노출하지 마세요.
내부 추론이나 thought 태그를 출력하지 말고 최종 답변만 출력하세요.{voice_instruction}

[현재 사용자]
{user.display_name} ({user.id})

[사용자 장기 memory]
{user_summary or "아직 없음"}

[서버 공유 context 요약]
{guild_summary or "아직 없음"}

[이 사용자와의 최근 대화]
{user_history or "아직 없음"}

[서버의 최근 공개 대화]
{guild_history or "아직 없음"}

[현재 요청]
{current_input}
""".strip()

    async def compress_if_needed(self, guild_id: int, user_id: int) -> None:
        async with self.compress_lock:
            user_rows = await self.store.user_rows_for_compression(guild_id, user_id)
            if user_rows:
                old_summary = await self.store.fetch_user_summary(guild_id, user_id)
                new_summary = await self.summarize_rows(
                    old_summary,
                    user_rows,
                    "사용자별 장기 memory를 갱신하세요. 취향, 선호, 진행 중인 일, 중요한 사실만 유지하세요.",
                )
                await self.store.update_user_summary(
                    guild_id, user_id, new_summary, [row["id"] for row in user_rows]
                )

            guild_rows = await self.store.guild_rows_for_compression(guild_id)
            if guild_rows:
                old_summary = await self.store.fetch_guild_summary(guild_id)
                new_summary = await self.summarize_rows(
                    old_summary,
                    guild_rows,
                    "서버 공유 context를 압축하세요. 여러 사용자 사이의 주제, 합의, 진행 상황만 남기세요.",
                )
                await self.store.update_guild_summary(
                    guild_id, new_summary, [row["id"] for row in guild_rows]
                )

    async def safe_compress_if_needed(self, guild_id: int, user_id: int) -> None:
        try:
            await self.compress_if_needed(guild_id, user_id)
        except Exception as exc:
            print(f"Context compression failed for guild={guild_id} user={user_id}: {exc}")

    async def summarize_rows(
        self, old_summary: str, rows: list[sqlite3.Row], instruction: str
    ) -> str:
        transcript = "\n".join(
            f"- {row['author_name']}({row['role']}): {row['content']}" for row in rows
        )
        prompt = f"""
{instruction}
기존 요약과 새 대화를 합쳐 800자 이내 한국어 bullet 요약으로 반환하세요.
불필요한 잡담과 중복은 제거하세요.

[기존 요약]
{old_summary or "없음"}

[새 대화]
{transcript}
""".strip()
        return await self.client.generate(prompt)


class SupertonicSpeaker:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.tts = None
        self.lock = asyncio.Lock()
        self.output_dir = DATA_DIR / "tts"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def synthesize(self, guild_id: int, text: str) -> Path:
        text = re.sub(r"\s+", " ", text).strip()[:SUPERTONIC_MAX_CHARS]
        if not text:
            raise AIError("TTS로 읽을 텍스트가 없어요.")

        settings = await self.store.get_voice_settings(guild_id)
        out_path = self.output_dir / f"tts_{guild_id}_{int(time.time() * 1000)}.wav"

        async with self.lock:
            await asyncio.to_thread(self._synthesize_sync, guild_id, text, settings, out_path)
        return out_path

    def _load_tts(self):
        if self.tts is None:
            from supertonic import TTS

            self.tts = TTS(model="supertonic-3", auto_download=True)
        return self.tts

    def _synthesize_sync(
        self, guild_id: int, text: str, settings: dict[str, str | None], out_path: Path
    ) -> None:
        tts = self._load_tts()
        voice_style_path = settings.get("voice_style_path")
        if voice_style_path and Path(voice_style_path).exists():
            style = tts.get_voice_style_from_path(voice_style_path)
        else:
            style = tts.get_voice_style(settings.get("voice_name") or SUPERTONIC_DEFAULT_VOICE)
        wav_data, _duration = tts.synthesize(
            text,
            voice_style=style,
            lang=settings.get("lang") or SUPERTONIC_DEFAULT_LANG,
        )
        tts.save_audio(wav_data, str(out_path))

    async def validate_voice_style(self, style_path: Path) -> None:
        async with self.lock:
            await asyncio.to_thread(self._validate_voice_style_sync, style_path)

    def _validate_voice_style_sync(self, style_path: Path) -> None:
        tts = self._load_tts()
        tts.get_voice_style_from_path(style_path)

    async def validate_builtin_voice(self, voice_name: str) -> None:
        async with self.lock:
            await asyncio.to_thread(self._validate_builtin_voice_sync, voice_name)

    def _validate_builtin_voice_sync(self, voice_name: str) -> None:
        tts = self._load_tts()
        tts.get_voice_style(voice_name)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}

    def state_for(self, guild: discord.Guild) -> GuildMusicState:
        return self.states.setdefault(guild.id, GuildMusicState())

    async def extract_track(self, query: str) -> Track:
        loop = asyncio.get_running_loop()

        def extract() -> dict:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as extractor:
                return extractor.extract_info(query, download=False)

        try:
            data = await loop.run_in_executor(None, extract)
        except yt_dlp.utils.DownloadError as exc:
            raise MusicError("노래를 찾거나 읽을 수 없어요. URL이나 검색어를 확인해 주세요.") from exc
        except Exception as exc:
            raise MusicError("yt-dlp 처리 중 오류가 발생했어요.") from exc

        if not data:
            raise MusicError("검색 결과가 없어요.")

        if "entries" in data:
            entries = [entry for entry in data["entries"] if entry]
            if not entries:
                raise MusicError("검색 결과가 없어요.")
            data = entries[0]

        stream_url = data.get("url")
        title = data.get("title")
        if not stream_url or not title:
            raise MusicError("이 항목은 재생할 수 없어요.")

        return Track(
            query=query,
            title=title,
            stream_url=stream_url,
            webpage_url=data.get("webpage_url"),
        )

    async def connect_author(self, interaction: discord.Interaction) -> discord.VoiceClient:
        if interaction.guild is None:
            raise MusicError("서버 채널에서만 사용할 수 있어요.")

        voice_state = getattr(interaction.user, "voice", None)
        if voice_state is None or voice_state.channel is None:
            raise MusicError("먼저 음성 채널에 들어가 주세요.")

        voice_client = interaction.guild.voice_client
        if voice_client is None:
            if voice_recv is not None:
                return await voice_state.channel.connect(cls=voice_recv.VoiceRecvClient)
            return await voice_state.channel.connect()

        if voice_client.channel != voice_state.channel:
            await voice_client.move_to(voice_state.channel)

        return voice_client

    async def start_next(
        self, guild: discord.Guild, channel: discord.abc.Messageable
    ) -> None:
        state = self.state_for(guild)

        start_failed = False
        started_title = None

        async with state.lock:
            voice_client = guild.voice_client
            if voice_client is None or not voice_client.is_connected():
                state.current = None
                return

            if voice_client.is_playing() or voice_client.is_paused():
                return

            if not state.queue:
                state.current = None
                return

            track = state.queue.popleft()
            state.current = track

            try:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTIONS),
                    volume=state.volume,
                )
                voice_client.play(
                    source,
                    after=lambda error: asyncio.run_coroutine_threadsafe(
                        self.after_track(guild, channel, error), self.bot.loop
                    ),
                )
            except Exception as exc:
                state.current = None
                await channel.send(f"재생을 시작하지 못했어요: {exc}")
                start_failed = True
            else:
                started_title = track.title

        if start_failed:
            await self.start_next(guild, channel)
            return

        if started_title:
            await channel.send(f"지금 재생중: {started_title}")

    async def after_track(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        error: Exception | None,
    ) -> None:
        state = self.state_for(guild)
        state.current = None

        if error:
            await channel.send(f"재생 중 오류가 발생했어요: {error}")

        await self.start_next(guild, channel)

    def stop_playback(self, voice_client: discord.VoiceClient) -> None:
        if hasattr(voice_client, "stop_playing"):
            voice_client.stop_playing()
        else:
            voice_client.stop()

    @app_commands.command(name="play", description="노래를 대기열에 추가하고 재생합니다.")
    @app_commands.describe(query="URL 또는 검색어")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        try:
            await self.connect_author(interaction)
            track = await self.extract_track(query)
        except MusicError as exc:
            await interaction.followup.send(str(exc))
            return

        state = self.state_for(interaction.guild)
        state.queue.append(track)
        await interaction.followup.send(f"대기열에 추가했어요: {track.title}")
        await self.start_next(interaction.guild, interaction.channel)

    @app_commands.command(name="skip", description="현재 노래를 건너뜁니다.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        state = self.state_for(interaction.guild)
        voice_client = interaction.guild.voice_client

        if voice_client is None or not voice_client.is_connected():
            await send_interaction(interaction, "봇이 음성 채널에 연결되어 있지 않아요.")
            return

        if voice_client.is_playing() or voice_client.is_paused():
            self.stop_playback(voice_client)
            await send_interaction(interaction, "현재 노래를 건너뛰었어요.")
            return

        if state.queue:
            await self.start_next(interaction.guild, interaction.channel)
            await send_interaction(interaction, "다음 노래를 재생할게요.")
            return

        await send_interaction(interaction, "건너뛸 노래가 없어요.")

    @app_commands.command(name="queue", description="현재 대기열을 보여줍니다.")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        state = self.state_for(interaction.guild)

        if not state.current and not state.queue:
            await send_interaction(interaction, "대기열에 노래가 없어요.")
            return

        lines = []
        if state.current:
            lines.append(f"재생 중: {state.current.title}")

        lines.extend(
            f"{index}. {track.title}" for index, track in enumerate(state.queue, start=1)
        )
        await send_interaction(interaction, "현재 대기열:\n" + "\n".join(lines))

    @app_commands.command(name="stop", description="재생을 멈추고 대기열을 비웁니다.")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        state = self.state_for(interaction.guild)
        state.queue.clear()
        state.current = None

        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            await send_interaction(interaction, "봇이 음성 채널에 연결되어 있지 않아요.")
            return

        await voice_client.disconnect()
        await send_interaction(interaction, "재생을 멈추고 대기열을 비웠어요.")

    @app_commands.command(name="pause", description="현재 노래를 일시정지합니다.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            await send_interaction(interaction, "봇이 음성 채널에 연결되어 있지 않아요.")
            return

        if not voice_client.is_playing():
            await send_interaction(interaction, "일시정지할 노래가 없어요.")
            return

        voice_client.pause()
        await send_interaction(interaction, "음악을 일시정지했어요.")

    @app_commands.command(name="resume", description="일시정지된 노래를 다시 재생합니다.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            await send_interaction(interaction, "봇이 음성 채널에 연결되어 있지 않아요.")
            return

        if not voice_client.is_paused():
            await send_interaction(interaction, "다시 재생할 일시정지된 노래가 없어요.")
            return

        voice_client.resume()
        await send_interaction(interaction, "음악을 다시 재생했어요.")

    @app_commands.command(name="volume", description="음량을 0부터 200 사이로 설정합니다.")
    @app_commands.describe(volume="0부터 200 사이의 음량")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, volume: app_commands.Range[int, 0, 200]) -> None:
        state = self.state_for(interaction.guild)
        state.volume = volume / 100

        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.source and hasattr(voice_client.source, "volume"):
            voice_client.source.volume = state.volume

        await send_interaction(interaction, f"음량을 {volume}%로 변경했어요.")


if voice_recv is not None:

    class ConversationAudioSink(voice_recv.AudioSink):
        def __init__(self):
            super().__init__()
            self.buffers: dict[int, dict[str, object]] = {}
            self.lock = Lock()

        def wants_opus(self) -> bool:
            return False

        def write(self, user: discord.Member | discord.User | None, data) -> None:
            if user is None or user.bot or not getattr(data, "pcm", None):
                return

            now = time.monotonic()
            with self.lock:
                entry = self.buffers.setdefault(
                    user.id,
                    {
                        "user": user,
                        "data": bytearray(),
                        "started_at": now,
                        "last_at": now,
                    },
                )
                entry["data"].extend(data.pcm)
                entry["last_at"] = now

        def pop_ready(self, silence_seconds: float, min_bytes: int):
            now = time.monotonic()
            ready = []
            with self.lock:
                for user_id, entry in list(self.buffers.items()):
                    audio = entry["data"]
                    last_at = entry["last_at"]
                    if now - last_at < silence_seconds:
                        continue
                    self.buffers.pop(user_id, None)
                    if len(audio) >= min_bytes:
                        ready.append((entry["user"], bytes(audio)))
            return ready

        def cleanup(self) -> None:
            with self.lock:
                self.buffers.clear()

else:
    ConversationAudioSink = None


class AI(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        store: MemoryStore,
        conversation: AIConversationService,
        speaker: SupertonicSpeaker,
    ):
        self.bot = bot
        self.store = store
        self.conversation = conversation
        self.speaker = speaker
        self.voice_states: dict[int, VoiceConversationState] = {}
        self.voice_response_lock = asyncio.Lock()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        if message.content.strip():
            await self.store.add_message(
                message.guild.id,
                message.channel.id,
                message.author.id,
                message.author.display_name,
                "user",
                "ambient",
                message.content,
            )

        if self.bot.user is None or self.bot.user not in message.mentions:
            return

        try:
            content = self.strip_bot_mentions(message.content).strip()
            media_path = await self.first_supported_attachment(message)
            if not content:
                content = "나를 멘션한 사용자에게 자연스럽게 응답해줘."

            async with message.channel.typing():
                answer = await self.conversation.answer(
                    guild=message.guild,
                    channel_id=message.channel.id,
                    user=message.author,
                    content=content,
                    kind="direct",
                    media_path=media_path,
                )
        except AIError as exc:
            await message.reply(str(exc), mention_author=False)
            return

        if answer:
            await message.reply(answer[:1900], mention_author=False)

    def strip_bot_mentions(self, content: str) -> str:
        if self.bot.user is None:
            return content
        return (
            content.replace(f"<@{self.bot.user.id}>", "")
            .replace(f"<@!{self.bot.user.id}>", "")
            .strip()
        )

    async def first_supported_attachment(self, message: discord.Message) -> Path | None:
        if not message.attachments:
            return None

        attachment = message.attachments[0]
        suffix = Path(attachment.filename).suffix.lower()
        if suffix not in AUDIO_SUFFIXES | IMAGE_SUFFIXES:
            return None

        attachment_dir = DATA_DIR / "attachments"
        attachment_dir.mkdir(parents=True, exist_ok=True)
        out_path = attachment_dir / f"{message.id}_{attachment.filename}"
        await attachment.save(out_path)
        if suffix in AUDIO_SUFFIXES and suffix != ".wav":
            return await self.convert_audio_to_wav(out_path)
        return out_path

    async def convert_audio_to_wav(self, source_path: Path) -> Path:
        wav_path = source_path.with_suffix(".wav")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-ac",
            str(VOICE_CHANNELS),
            "-ar",
            str(VOICE_SAMPLE_RATE),
            "-f",
            "wav",
            str(wav_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise AIError(f"오디오 첨부 파일을 WAV로 변환하지 못했어요: {stderr.decode(errors='ignore')[:300]}")
        return wav_path

    @app_commands.command(name="context_reset", description="내 AI 대화 context와 장기 memory를 초기화합니다.")
    @app_commands.guild_only()
    async def context_reset(self, interaction: discord.Interaction) -> None:
        await self.store.reset_user(interaction.guild.id, interaction.user.id)
        await send_interaction(interaction, "내 대화 context와 장기 memory를 초기화했어요.", ephemeral=True)

    @app_commands.command(name="server_context_reset", description="서버 공유 context를 초기화합니다. 관리자 전용.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def server_context_reset(self, interaction: discord.Interaction) -> None:
        await self.store.reset_guild(interaction.guild.id)
        await send_interaction(interaction, "서버 공유 context를 초기화했어요.", ephemeral=True)

    @app_commands.command(name="voice_join", description="음성 채널에 들어가 말로 대화합니다.")
    @app_commands.describe(always_reply="켜면 들은 말마다 답하고, 끄면 봇에게 한 말일 때만 답하려고 시도합니다.")
    @app_commands.guild_only()
    async def voice_join(
        self, interaction: discord.Interaction, always_reply: bool = True
    ) -> None:
        if voice_recv is None or ConversationAudioSink is None:
            await send_interaction(
                interaction,
                "`discord-ext-voice-recv`가 설치되어 있지 않아 음성 수신을 사용할 수 없어요.",
                ephemeral=True,
            )
            return

        voice_state = getattr(interaction.user, "voice", None)
        if voice_state is None or voice_state.channel is None:
            await send_interaction(interaction, "먼저 음성 채널에 들어가 주세요.", ephemeral=True)
            return

        voice_client = interaction.guild.voice_client
        if voice_client is not None and not isinstance(voice_client, voice_recv.VoiceRecvClient):
            await voice_client.disconnect(force=True)
            voice_client = None

        if voice_client is None:
            voice_client = await voice_state.channel.connect(cls=voice_recv.VoiceRecvClient)
        elif voice_client.channel != voice_state.channel:
            await voice_client.move_to(voice_state.channel)

        old_state = self.voice_states.pop(interaction.guild.id, None)
        if old_state:
            old_state.monitor_task.cancel()
            if voice_client.is_listening():
                voice_client.stop_listening()

        sink = ConversationAudioSink()
        monitor_task = asyncio.create_task(self.monitor_voice(interaction.guild.id))
        self.voice_states[interaction.guild.id] = VoiceConversationState(
            text_channel_id=interaction.channel.id,
            sink=sink,
            monitor_task=monitor_task,
            always_reply=always_reply,
        )
        voice_client.listen(sink)
        await send_interaction(interaction, "음성 대화를 시작했어요. 이제 말로 대화할 수 있어요.")

    @app_commands.command(name="voice_leave", description="음성 대화를 종료하고 채널에서 나갑니다.")
    @app_commands.guild_only()
    async def voice_leave(self, interaction: discord.Interaction) -> None:
        state = self.voice_states.pop(interaction.guild.id, None)
        if state:
            state.monitor_task.cancel()

        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)
            await send_interaction(interaction, "음성 대화를 종료하고 나갔어요.")
            return
        await send_interaction(interaction, "봇이 음성 채널에 연결되어 있지 않아요.")

    @app_commands.command(name="voice_style", description="TTS 기본 음성과 언어를 설정합니다. 관리자 전용.")
    @app_commands.describe(voice_name="M1-M5 또는 F1-F5", lang="언어 코드. 한국어는 ko")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def voice_style(
        self,
        interaction: discord.Interaction,
        voice_name: str = SUPERTONIC_DEFAULT_VOICE,
        lang: str = SUPERTONIC_DEFAULT_LANG,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        voice_name = voice_name.upper()
        try:
            await self.speaker.validate_builtin_voice(voice_name)
        except Exception as exc:
            await interaction.followup.send(f"해당 음성을 불러오지 못했어요: {exc}", ephemeral=True)
            return

        await self.store.set_voice_settings(
            interaction.guild.id, voice_name=voice_name, voice_style_path="", lang=lang
        )
        await interaction.followup.send(f"TTS 음성을 {voice_name}, 언어를 {lang}로 설정했어요.", ephemeral=True)

    @app_commands.command(name="voice_refer", description="Supertonic voice style JSON을 적용합니다. 관리자 전용.")
    @app_commands.describe(refer="Voice Builder JSON 또는 로컬 변환기에 넣을 reference audio")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def voice_refer(
        self, interaction: discord.Interaction, refer: discord.Attachment
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        suffix = Path(refer.filename).suffix.lower()
        refer_dir = DATA_DIR / "voice_refs" / str(interaction.guild.id)
        refer_dir.mkdir(parents=True, exist_ok=True)
        saved_path = refer_dir / f"{int(time.time())}_{refer.filename}"
        await refer.save(saved_path)

        if suffix == ".json":
            try:
                await self.speaker.validate_voice_style(saved_path)
            except Exception as exc:
                await interaction.followup.send(f"voice style JSON을 읽지 못했어요: {exc}", ephemeral=True)
                return

            await self.store.set_voice_settings(
                interaction.guild.id, voice_style_path=str(saved_path)
            )
            await interaction.followup.send("업로드한 Supertonic voice style JSON을 적용했어요.", ephemeral=True)
            return

        if suffix not in {".wav", ".mp3", ".m4a", ".ogg", ".flac"}:
            await interaction.followup.send("JSON 또는 오디오 파일만 업로드할 수 있어요.", ephemeral=True)
            return

        if not SUPERTONIC_VOICE_BUILDER_CMD:
            await interaction.followup.send(
                "reference audio는 저장했지만, 공개 Supertonic ONNX SDK에는 오디오를 voice style JSON으로 바꾸는 "
                "온디바이스 변환기가 포함되어 있지 않아요. `SUPERTONIC_VOICE_BUILDER_CMD`를 설정하거나 "
                "Voice Builder에서 만든 Supertonic 3 JSON을 업로드해 주세요.",
                ephemeral=True,
            )
            return

        output_json = saved_path.with_suffix(".json")
        command = SUPERTONIC_VOICE_BUILDER_CMD.format(
            input=shlex.quote(str(saved_path)), output=shlex.quote(str(output_json))
        )
        proc = await asyncio.create_subprocess_shell(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0 or not output_json.exists():
            await interaction.followup.send(
                "reference audio 변환에 실패했어요:\n"
                f"{stderr.decode(errors='ignore')[:1200] or stdout.decode(errors='ignore')[:1200]}",
                ephemeral=True,
            )
            return

        try:
            await self.speaker.validate_voice_style(output_json)
        except Exception as exc:
            await interaction.followup.send(f"변환된 voice style JSON을 읽지 못했어요: {exc}", ephemeral=True)
            return

        await self.store.set_voice_settings(interaction.guild.id, voice_style_path=str(output_json))
        await interaction.followup.send("reference audio로 만든 voice style을 적용했어요.", ephemeral=True)

    async def monitor_voice(self, guild_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                state = self.voice_states.get(guild_id)
                if state is None:
                    return
                for user, pcm in state.sink.pop_ready(VOICE_SILENCE_SECONDS, VOICE_MIN_BYTES):
                    if user.id in state.processing_users:
                        continue
                    state.processing_users.add(user.id)
                    asyncio.create_task(self.handle_voice_utterance(guild_id, user, pcm))
        except asyncio.CancelledError:
            return

    async def handle_voice_utterance(
        self, guild_id: int, user: discord.Member | discord.User, pcm: bytes
    ) -> None:
        state = self.voice_states.get(guild_id)
        guild = self.bot.get_guild(guild_id)
        if state is None or guild is None:
            return

        audio_dir = DATA_DIR / "voice_inputs"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_dir / f"voice_{guild_id}_{user.id}_{int(time.time() * 1000)}.wav"
        self.write_pcm_wav(audio_path, pcm)

        channel = guild.get_channel(state.text_channel_id)
        try:
            async with self.voice_response_lock:
                answer = await self.conversation.answer(
                    guild=guild,
                    channel_id=state.text_channel_id,
                    user=user,
                    content="[voice input]",
                    kind="voice",
                    media_path=audio_path,
                    always_reply=state.always_reply,
                )
                if not answer:
                    return
                if channel is not None:
                    await channel.send(f"{user.display_name}: {answer[:1700]}")
                await self.play_tts(guild, answer, channel)
        except AIError as exc:
            if channel is not None:
                await channel.send(str(exc))
        finally:
            fresh_state = self.voice_states.get(guild_id)
            if fresh_state:
                fresh_state.processing_users.discard(user.id)

    def write_pcm_wav(self, path: Path, pcm: bytes) -> None:
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(VOICE_CHANNELS)
            wav_file.setsampwidth(VOICE_SAMPLE_WIDTH)
            wav_file.setframerate(VOICE_SAMPLE_RATE)
            wav_file.writeframes(pcm)

    async def play_tts(
        self,
        guild: discord.Guild,
        answer: str,
        channel: discord.abc.Messageable | None,
    ) -> None:
        voice_client = guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            return
        if voice_client.is_playing() or voice_client.is_paused():
            if channel is not None:
                await channel.send("현재 오디오가 재생 중이라 TTS 음성 출력은 건너뛰었어요.")
            return

        try:
            audio_path = await self.speaker.synthesize(guild.id, answer)
        except Exception as exc:
            if channel is not None:
                await channel.send(f"TTS 생성에 실패했어요: {exc}")
            return

        done = asyncio.Event()

        def after_play(error: Exception | None) -> None:
            if error and channel is not None:
                asyncio.run_coroutine_threadsafe(
                    channel.send(f"TTS 재생 중 오류가 발생했어요: {error}"), self.bot.loop
                )
            self.bot.loop.call_soon_threadsafe(done.set)

        voice_client.play(discord.FFmpegPCMAudio(str(audio_path)), after=after_play)
        try:
            await asyncio.wait_for(done.wait(), timeout=180)
        except asyncio.TimeoutError:
            if hasattr(voice_client, "stop_playing"):
                voice_client.stop_playing()
            else:
                voice_client.stop()


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    description="HD Music Bot",
    intents=intents,
)
bot.synced_commands = False

memory_store = MemoryStore(MEMORY_DB_PATH)
ollama_client = OllamaClient(OLLAMA_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT)
conversation_service = AIConversationService(memory_store, ollama_client)
supertonic_speaker = SupertonicSpeaker(memory_store)


@bot.event
async def on_ready() -> None:
    if not bot.synced_commands:
        if SYNC_GUILD_ID:
            guild_object = discord.Object(id=int(SYNC_GUILD_ID))
            bot.tree.copy_global_to(guild=guild_object)
            synced = await bot.tree.sync(guild=guild_object)
            print(f"Synced {len(synced)} guild slash commands to {SYNC_GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global slash commands.")
        bot.synced_commands = True

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await send_interaction(interaction, "이 명령어는 서버 관리자만 사용할 수 있어요.", ephemeral=True)
        return
    await send_interaction(interaction, f"명령어 처리 중 오류가 발생했어요: {error}", ephemeral=True)


async def handle_health_check(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        await asyncio.wait_for(reader.read(1024), timeout=1)
    except asyncio.TimeoutError:
        pass

    body = b"OK"
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def start_health_server() -> asyncio.Server | None:
    if not PORT:
        return None

    try:
        port = int(PORT)
    except ValueError as exc:
        raise RuntimeError("PORT must be a number.") from exc

    server = await asyncio.start_server(handle_health_check, HEALTH_HOST, port)
    print(f"Health check listening on {HEALTH_HOST}:{port}")
    return server


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable is required.")

    health_server = await start_health_server()
    try:
        async with bot:
            await bot.add_cog(Music(bot))
            await bot.add_cog(
                AI(bot, memory_store, conversation_service, supertonic_speaker)
            )
            await bot.start(TOKEN)
    finally:
        if health_server:
            health_server.close()
            await health_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
