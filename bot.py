import asyncio
import os
from collections import deque
from dataclasses import dataclass, field

import discord
import yt_dlp
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
OPUS_LIBRARY_PATH = os.getenv("OPUS_LIBRARY_PATH")
PORT = os.getenv("PORT")
HEALTH_HOST = os.getenv("HEALTH_HOST", "0.0.0.0")

if OPUS_LIBRARY_PATH:
    discord.opus.load_opus(OPUS_LIBRARY_PATH)

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


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("서버 채널에서만 사용할 수 있어요.")
            return False
        return True

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CheckFailure):
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("사용법을 확인해 주세요: `!play <URL 또는 검색어>`, `!volume <0-200>`")
            return

        if isinstance(error, commands.BadArgument):
            await ctx.send("명령어 인자를 확인해 주세요.")
            return

        raise error

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

    async def connect_author(self, ctx: commands.Context) -> discord.VoiceClient:
        if ctx.guild is None:
            raise MusicError("서버 채널에서만 사용할 수 있어요.")

        voice_state = getattr(ctx.author, "voice", None)
        if voice_state is None or voice_state.channel is None:
            raise MusicError("먼저 음성 채널에 들어가 주세요.")

        if ctx.voice_client is None:
            return await voice_state.channel.connect()

        if ctx.voice_client.channel != voice_state.channel:
            await ctx.voice_client.move_to(voice_state.channel)

        return ctx.voice_client

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

    @commands.command(aliases=["p"])
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        """Add a song to this server's queue."""
        try:
            await self.connect_author(ctx)
            track = await self.extract_track(query)
        except MusicError as exc:
            await ctx.send(str(exc))
            return

        state = self.state_for(ctx.guild)
        state.queue.append(track)
        await ctx.send(f"대기열에 추가했어요: {track.title}")
        await self.start_next(ctx.guild, ctx.channel)

    @commands.command(name="skip", aliases=["n", "next"])
    async def skip(self, ctx: commands.Context) -> None:
        """Skip the current song."""
        state = self.state_for(ctx.guild)
        voice_client = ctx.voice_client

        if voice_client is None or not voice_client.is_connected():
            await ctx.send("봇이 음성 채널에 연결되어 있지 않아요.")
            return

        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
            await ctx.send("현재 노래를 건너뛰었어요.")
            return

        if state.queue:
            await self.start_next(ctx.guild, ctx.channel)
            return

        await ctx.send("건너뛸 노래가 없어요.")

    @commands.command(name="queue", aliases=["pl", "playlist"])
    async def queue(self, ctx: commands.Context) -> None:
        """Show this server's queue."""
        state = self.state_for(ctx.guild)

        if not state.current and not state.queue:
            await ctx.send("대기열에 노래가 없어요.")
            return

        lines = []
        if state.current:
            lines.append(f"재생 중: {state.current.title}")

        lines.extend(
            f"{index}. {track.title}" for index, track in enumerate(state.queue, start=1)
        )
        await ctx.send("현재 대기열:\n" + "\n".join(lines))

    @commands.command()
    async def stop(self, ctx: commands.Context) -> None:
        """Stop playback and clear this server's queue."""
        state = self.state_for(ctx.guild)
        state.queue.clear()
        state.current = None

        if ctx.voice_client is None or not ctx.voice_client.is_connected():
            await ctx.send("봇이 음성 채널에 연결되어 있지 않아요.")
            return

        await ctx.voice_client.disconnect()
        await ctx.send("재생을 멈추고 대기열을 비웠어요.")

    @commands.command()
    async def pause(self, ctx: commands.Context) -> None:
        """Pause playback."""
        if ctx.voice_client is None or not ctx.voice_client.is_connected():
            await ctx.send("봇이 음성 채널에 연결되어 있지 않아요.")
            return

        if not ctx.voice_client.is_playing():
            await ctx.send("일시정지할 노래가 없어요.")
            return

        ctx.voice_client.pause()
        await ctx.send("음악을 일시정지했어요.")

    @commands.command()
    async def resume(self, ctx: commands.Context) -> None:
        """Resume playback."""
        if ctx.voice_client is None or not ctx.voice_client.is_connected():
            await ctx.send("봇이 음성 채널에 연결되어 있지 않아요.")
            return

        if not ctx.voice_client.is_paused():
            await ctx.send("다시 재생할 일시정지된 노래가 없어요.")
            return

        ctx.voice_client.resume()
        await ctx.send("음악을 다시 재생했어요.")

    @commands.command(aliases=["v"])
    async def volume(self, ctx: commands.Context, volume: int) -> None:
        """Set playback volume from 0 to 200."""
        if not 0 <= volume <= 200:
            await ctx.send("음량은 0부터 200 사이로 입력해 주세요.")
            return

        state = self.state_for(ctx.guild)
        state.volume = volume / 100

        if ctx.voice_client and ctx.voice_client.source:
            ctx.voice_client.source.volume = state.volume

        await ctx.send(f"음량을 {volume}%로 변경했어요.")


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("!"),
    description="HD Music Bot",
    intents=intents,
)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


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
            await bot.start(TOKEN)
    finally:
        if health_server:
            health_server.close()
            await health_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
