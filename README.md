# uTau-Tomo

A Discord slash-command music bot with configurable local LLM chat memory, voice-channel conversation, and CPU Supertonic 3 TTS.

## Setup

1. Install Python 3.10 or newer.
2. Install FFmpeg and opus.
3. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

4. Copy `.env.example` to `.env` and fill in at least `DISCORD_TOKEN`.
5. Run the bot:

```bash
python3 bot.py
```

`PORT` is optional. When set, the bot opens a small HTTP health check that returns `OK`.

The STN newsroom collector is a separate worker so browser crawling and scheduled
publishing cannot block music playback:

```bash
python3 -m newsroom_worker check
python3 -m newsroom_worker once all
python3 -m newsroom_worker run
```

## Environment

```bash
DISCORD_TOKEN=your-bot-token

# Optional, but recommended while developing so slash commands sync instantly to one server.
DISCORD_GUILD_ID=your-test-server-id

# Optional opus path if auto-loading fails.
OPUS_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libopus.so.0

# OpenAI-compatible LLM backend.
LLM_BACKEND=openai
LLM_URL=https://monapi.kotori9.run/v1
LLM_MODEL=deepseek-v4.1-flash
LLM_API_KEY=your-llm-api-key
LLM_TIMEOUT=180
LLM_MAX_TOKENS=2048
LLM_TOOL_MAX_STEPS=2
LLM_FALLBACK_ENABLED=false
LLM_FALLBACK_BACKEND=ollama
LLM_FALLBACK_URL=http://192.168.0.197:11434
LLM_FALLBACK_MODEL=Qwythos-v2-9B:Q5
LLM_FALLBACK_TIMEOUT=180
AI_PROMPT_PATH=ai_prompt.md

# Optional only when LLM_BACKEND=nim. LLM_API_KEY takes precedence.
# NIM_TOKEN=your-nvidia-nim-token

# Web search for AI answers
WEB_SEARCH_ENABLED=true
WEB_SEARCH_MAX_RESULTS=3

# Local data and memory
BOT_DATA_DIR=data
MEMORY_DB_PATH=data/memory.sqlite3
MAX_PLAYLIST_TRACKS=100
VOICE_EMPTY_DISCONNECT_SECONDS=30

# YouTube stream client used by yt-dlp. Keep this as android unless needed.
YTDL_YOUTUBE_PLAYER_CLIENT=android

# STN newsroom worker (reuses the Discord and LLM credentials above)
NEWSROOM_CONFIG_PATH=config/newsroom.json
NEWSROOM_STATE_PATH=data/newsroom/state.json
NEWSROOM_REPO_STATE_PATH=data/newsroom/repo-state.json
NEWSROOM_EMBEDDING_MODEL=embeddinggemma-2
NEWSROOM_SUMMARY_MODEL=deepseek-v4.1-flash
NEWSROOM_SUMMARY_MAX_TOKENS=2048
NEWSROOM_KNOWLEDGE_THRESHOLD=0.62
NEWSROOM_KNOWLEDGE_MARGIN=0.035
NEWSROOM_BROWSER_HEADLESS=true
NEWSROOM_DRY_RUN=false

# Supertonic 3 TTS
SUPERTONIC_DEFAULT_VOICE=M4
SUPERTONIC_DEFAULT_LANG=ko
SUPERTONIC_MAX_CHARS=700

# Optional: command that converts reference audio into a Supertonic 3 voice JSON.
# Use {input} and {output}; the command is only invoked by the admin-only /voice_refer command.
# SUPERTONIC_VOICE_BUILDER_CMD=/path/to/build_voice --input {input} --output {output}
```

## Slash Commands

- `/play query video:false`: add a song or a YouTube playlist to the queue. `video` defaults to `false`; set it to `true` to show the YouTube link/embed for only the tracks added by that command when they begin playing.
- `/watch query`: stop the music queue, select a YouTube video, and launch the Discord Activity for the caller's voice channel.
- `/skip`: vote to skip the current song. Only users in the bot's current voice channel can vote. The majority threshold is based on non-bot, non-admin listeners; administrators skip immediately.
- `/back`: go back to the previous song. Administrator only.
- `/seek timestamp`: jump the current video/song to a timestamp such as `90`, `1:30`, `01:02:03`, or `1m30s`.
- `/queue`: show the current song and queued songs.
- `/queue_remove index`: vote to remove a specific queued song by its `/queue` number. The threshold is 30% of non-bot listeners in the bot's current voice channel; administrators remove immediately.
- `/queue_clear`: clear queued songs while keeping the current song playing. Administrator only.
- `/stop`: stop playback, clear the queue, and disconnect.
- `/pause`: pause the current song.
- `/resume`: resume a paused song.
- `/volume volume`: set playback volume from 0 to 200.
Discord's standard bot voice API sends audio only; it cannot start a Go Live screen share. `video:true` therefore posts the YouTube link with its Discord preview, so participants can open the same video together. A person in the channel must start an actual screen share.
- `/context_reset`: reset your personal AI context and long-term memory in this server.
- `/server_context_reset`: reset shared server context. Administrator only.
- `/llm_toggle enabled`: turn mention/voice LLM responses on or off for this server. Administrator only.
- Mention-chat moderation: administrators can ask the LLM to timeout, remove timeout, kick, ban, or unban a user. The bot executes this only through the admin-only `discord_moderation` tool after checking Discord permissions and role hierarchy.
- `/voice_join always_reply`: join your voice channel, listen, answer with the configured LLM, and speak using Supertonic TTS.
- `/voice_leave`: vote to stop voice conversation and leave. Only users in the bot's current voice channel can vote, and a majority of non-bot listeners must agree. Administrators leave immediately.
- `/tts_message text play_in_voice`: generate a WAV voice message with the configured TTS voice. Optionally also play it in your voice channel.
- `/voice_style voice_name lang`: set built-in Supertonic voice and language. Administrator only.
- `/voice_refer refer`: apply a Supertonic voice style JSON, or run an optional reference-audio-to-JSON converter. Administrator only.
- `/worldcup url data rounds`: start a PIKU-style ideal type worldcup in the current channel. Users join with a button, then every joined participant must agree to start. During matches, every joined participant votes on each `n강 A vs B` match. Channel chat counts as activity while the game is running, so discussion does not trigger the 5-minute timeout.
- `/worldcup_export_script`: get a browser-side export script for PIKU pages that block direct bot access.
- `/quiz url data count`: start a machugi.io competitive quiz in the current channel. `count=0` uses the largest question count offered by the page. Users join with a button, every joined participant must agree to start, then answers are accepted in chat. The first participant to type an accepted answer gets the point.


## Watch Together Activity

`/watch` launches this app's Discord Activity instead of trying to screen-share from the bot account. Everyone in the voice channel gets a YouTube player inside Discord; play, pause, and 10-second seeks are synchronized through the bot service. The bot stops its music queue first so the audio is not played twice.

Before using it, serve this project at a public HTTPS hostname and set `ACTIVITY_PUBLIC_URL` to that base URL. In the Developer Portal, enable **Activities** and set the Root Mapping to prefix `/` and that hostname (without `https://`). Then add these Proxy Path Mappings before the `/` mapping: `/youtube` → `www.youtube.com`, and `/jsdelivr` → `cdn.jsdelivr.net`. Restart the bot after changing the environment variable.

The standard Discord bot voice API still cannot start a Go Live screen share. Do not use a user-token/self-bot workaround; the Activity is the supported co-watching implementation.
## Party Games

`/quiz` directly parses machugi.io quiz pages and calls the site's question API with the original answer arrays. Image, video, audio, and text questions are shown in Discord when the source data includes those media URLs.

`/worldcup` tries to parse PIKU pages directly. PIKU may return a Cloudflare block to server-side bot requests. In that case, use `/worldcup_export_script`, open the PIKU page in your normal browser, press `시작하기` yourself, run the script on the active A vs B match screen, then upload the downloaded `piku-worldcup.json` with the `data` option.

If the script extracts too few items, run it again on the active A vs B match screen and enter the desired first-round size when prompted. The script scans page state, browser storage, visible media elements, and CSS background images, then advances the original PIKU match to collect candidates from the first round.

Worldcup fallback JSON:

```json
{
  "title": "내 월드컵",
  "items": [
    {"name": "A 후보", "media_url": "https://example.com/a.jpg"},
    {"name": "B 후보", "media_url": "https://example.com/b.jpg"}
  ]
}
```

Quiz fallback JSON:

```json
{
  "title": "내 퀴즈",
  "questions": [
    {
      "prompt": "문제 설명",
      "media_url": "https://example.com/question.jpg",
      "media_type": "image",
      "answers": ["정답", "별칭"]
    }
  ]
}
```

Only one party game can run in a channel at a time. Quiz skips require every joined participant to agree. If there is no interaction for 5 minutes, the game ends automatically.

## Mention Chat

Mention the bot in any server text channel to talk through the configured LLM backend. The bot keeps:

- per-user recent context;
- per-user long-term memory;
- automatic compression into SQLite summaries.

Example:

```text
@HD Music Bot 오늘 대화 이어서 생각해줘
```

Audio/image attachments on a mention are passed to Ollama when `LLM_BACKEND=ollama`.

For the local Ollama setup, the default prompt includes only the current user's memory plus the last 3 direct messages and limits tool loops. It does not inject other users' context or broad server chat history. When the model needs that information, it can call tools:

- `web_search`: current external information through `ddgs`.
- `discord_context`: server overview, public member info, recent Discord channel messages, DB-backed message history, user memory, or server memory.
- `music_queue`: current music queue.
- `music_recommend`: YouTube music recommendation and queue insertion.
- `ask_user`: ask a short follow-up question.

Tools are registered through `ToolRegistry`; add a new `AITool` subclass and register it near startup to extend the harness.

AI replies stream into Discord by sending one placeholder reply and editing that same message about once per second while the backend streams tokens.

The bot also reads `ai_prompt.md` on each AI request. Keep this file short and English; the prompt tells the model to answer in Korean.

If you mention the bot and ask for music recommendations, the model must explicitly call the `music_recommend` tool. It should ask for genre, topic, mood, language, and similar preferences with `ask_user` when needed. Once it has enough detail, the bot searches YouTube directly from the tool query and adds matching videos to the music queue automatically without an extra LLM query-generation call. Join a voice channel first so the bot can connect and start playback.

## Voice And TTS Notes

Voice receiving uses `discord-ext-voice-recv`, because `discord.py` itself only sends audio. Supertonic 3 runs locally on CPU through the `supertonic` Python SDK and downloads its model assets on first use.

The open Supertonic ONNX SDK can load built-in voices and Supertonic voice-style JSON files. It does not ship a local reference-audio-to-voice-style converter. For voice cloning, upload a Voice Builder Supertonic 3 JSON with `/voice_refer`, or configure `SUPERTONIC_VOICE_BUILDER_CMD` if you have a local converter that produces that JSON from an audio reference.

If music or another audio source is already playing, the bot sends the AI answer as text and skips TTS playback so it does not destroy the music stream.

When the bot is alone in a voice channel for `VOICE_EMPTY_DISCONNECT_SECONDS`, it stops playback, clears the music state, and leaves automatically.

## Discord Developer Portal Checklist

1. Enable the bot scopes `bot` and `applications.commands` in the install URL.
2. Grant bot permissions for slash commands, sending messages, reading message history, connecting to voice, speaking in voice, and using voice activity.
3. Enable Message Content Intent if you want mention chat to see full message text reliably.
4. Invite or re-invite the bot with the updated install URL.
5. During development, set `DISCORD_GUILD_ID` to your server ID for fast command sync. Remove it later for global sync.

## LLM Check

The bot calls:

```bash
curl https://monapi.kotori9.run/v1/chat/completions \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"Reply with OK"}]}'
```

Keep the API key in `.env`; do not put the real credential in tracked files.

## STN Newsroom Worker

`newsroom_worker` checks RSS/Atom feeds and Korean Google News search feeds every
10 minutes. Sources include Ars Technica, The Verge, BleepingComputer, The Hacker
News, Hugging Face and arXiv; search feeds discover articles from additional
publishers. Samsung browser collectors run hourly. Channel IDs,
feeds, intervals, routing anchors and limits live in `config/newsroom.json`.
Repository update notifications are disabled.

General news is routed by `embeddinggemma-2` using its title and content against
the technology, AI, hardware, security and research anchor sets. The highest
cosine similarity selects the channel, subject to a minimum score; source names
and keyword rules do not select the channel. Failed embeddings defer publication.
The worker publishes at most 4 items per cycle, 2 per source per cycle and 20
per UTC day. These are ceilings, not quotas. Only articles from the last 24 hours
with valid dates are considered; missing and future dates are rejected.
New feeds are seeded without backfill. URLs and normalized title hashes prevent
exact duplicates. `embeddinggemma-2` compares candidate content against seven
days of recent posts, including feed-channel bot messages imported hourly.
An editorial DeepSeek check then requires useful, concrete new facts: translated
reprints, recap articles, introductory tutorials, speculation and promotional
pieces are rejected. Material updates to an earlier story can pass when they
add a new patch, confirmed impact or research result. Model errors defer posting.
Rejected URLs are cached; failed publications remain eligible for retry.

The two state files are written through a temporary file and `os.replace`.
On the first run, or after unreadable state, the worker seeds the current URLs,
commits, and tags without posting them. A Discord message is recorded only after
the API accepts it, so failed posts are retried on the next scheduled run.

Samsung articles must pass both the hard promotion-title rule and the
`embeddinggemma-2` similarity filter. Summaries use `deepseek-v4.1-flash`, are
limited to 120 Korean characters, and are not posted if summarization fails.
The worker checks public Threads posts from `@choi.openai` each cycle and
forwards each newly seen post with a verified timestamp within the last 24
hours to the AI news channel. Delivery uses a source excerpt and original link,
with URL deduplication. The feed bypasses editorial filters, model calls and
general news quotas.
A login wall skips the source for that run.

The configured gateway counts hidden reasoning inside `max_tokens`, so the
worker streams with a 2048-token completion budget to obtain final content while
discarding reasoning fields. Only the validated final summary is retained, and
the published text remains hard-limited to 120 characters.

Before starting the service, run:

```bash
python3 -m newsroom_worker check
python3 -m unittest discover -s tests -v
python3 -m scripts.calibrate_newsroom
```

The check verifies the embedding endpoint and the bot's effective Discord
permissions. Each feed channel must allow only the operational permissions it
needs (`View Channel`, `Send Messages`, `Embed Links`, and `Read Message
History`); it must not grant `Manage Channels`.

The production unit template is `deploy/hd-newsroom-worker.service`. Install it
as a user service and inspect logs with:

```bash
systemctl --user enable --now hd-newsroom-worker.service
journalctl --user -u hd-newsroom-worker.service -f
```

Rollback is immediate: stop the newsroom unit. To roll back the music bot too,
deploy the previous Git commit and restart `hd-music-bot.service`; both state
files remain forward-compatible JSON and should be preserved.
