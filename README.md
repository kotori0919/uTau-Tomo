# HD Music Bot

A Discord slash-command music bot with configurable local LLM chat memory, voice-channel conversation, and CPU Supertonic 3 TTS.

## Setup

1. Install Python 3.10 or newer.
2. Install FFmpeg and opus.
3. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and fill in at least `DISCORD_TOKEN`.
5. Run the bot:

```bash
python3 bot.py
```

`PORT` is optional. When set, the bot opens a small HTTP health check that returns `OK`.

## Environment

```bash
DISCORD_TOKEN=your-bot-token

# Optional, but recommended while developing so slash commands sync instantly to one server.
DISCORD_GUILD_ID=your-test-server-id

# Optional opus path if auto-loading fails.
OPUS_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libopus.so.0

# LLM backend. The default setup uses Ollama on the local network.
LLM_BACKEND=ollama
LLM_URL=http://192.168.0.42:11434
LLM_MODEL=SuperQwen-AgentWorld-35B-A3B:Q4
LLM_TIMEOUT=180
LLM_MAX_TOKENS=2048
LLM_TOOL_MAX_STEPS=2
LLM_FALLBACK_ENABLED=false
LLM_FALLBACK_BACKEND=ollama
LLM_FALLBACK_URL=http://192.168.0.42:11434
LLM_FALLBACK_MODEL=SuperQwen-AgentWorld-35B-A3B:Q4
LLM_FALLBACK_TIMEOUT=180
AI_PROMPT_PATH=ai_prompt.md

# Optional only when LLM_BACKEND=nim.
# NIM_TOKEN=your-nvidia-nim-token

# Web search for AI answers
WEB_SEARCH_ENABLED=true
WEB_SEARCH_MAX_RESULTS=3

# Local data and memory
BOT_DATA_DIR=data
MEMORY_DB_PATH=data/memory.sqlite3
MAX_PLAYLIST_TRACKS=100
VOICE_EMPTY_DISCONNECT_SECONDS=30

# Supertonic 3 TTS
SUPERTONIC_DEFAULT_VOICE=M4
SUPERTONIC_DEFAULT_LANG=ko
SUPERTONIC_MAX_CHARS=700

# Optional: command that converts reference audio into a Supertonic 3 voice JSON.
# Use {input} and {output}; the command is only invoked by the admin-only /voice_refer command.
# SUPERTONIC_VOICE_BUILDER_CMD=/path/to/build_voice --input {input} --output {output}
```

## Slash Commands

- `/play query`: add a song or a YouTube playlist to the queue. If nothing is playing, it starts playback.
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
- `/context_reset`: reset your personal AI context and long-term memory in this server.
- `/server_context_reset`: reset shared server context. Administrator only.
- `/llm_toggle enabled`: turn mention/voice LLM responses on or off for this server. Administrator only.
- `/voice_join always_reply`: join your voice channel, listen, answer with the configured LLM, and speak using Supertonic TTS.
- `/voice_leave`: vote to stop voice conversation and leave. Only users in the bot's current voice channel can vote, and a majority of non-bot listeners must agree. Administrators leave immediately.
- `/tts_message text play_in_voice`: generate a WAV voice message with the configured TTS voice. Optionally also play it in your voice channel.
- `/voice_style voice_name lang`: set built-in Supertonic voice and language. Administrator only.
- `/voice_refer refer`: apply a Supertonic voice style JSON, or run an optional reference-audio-to-JSON converter. Administrator only.
- `/worldcup url data rounds`: start a PIKU-style ideal type worldcup in the current channel. Users join with a button, then every joined participant must agree to start. During matches, every joined participant votes on each `n강 A vs B` match. Channel chat counts as activity while the game is running, so discussion does not trigger the 5-minute timeout.
- `/worldcup_export_script`: get a browser-side export script for PIKU pages that block direct bot access.
- `/quiz url data count`: start a machugi.io competitive quiz in the current channel. `count=0` uses the largest question count offered by the page. Users join with a button, every joined participant must agree to start, then answers are accepted in chat. The first participant to type an accepted answer gets the point.

## Party Games

`/quiz` directly parses machugi.io quiz pages and calls the site's question API with the original answer arrays. Image, video, audio, and text questions are shown in Discord when the source data includes those media URLs.

`/worldcup` tries to parse PIKU pages directly. PIKU may return a Cloudflare block to server-side bot requests. In that case, use `/worldcup_export_script`, open the PIKU page in your normal browser, run the script in the browser console after the candidates are loaded, then upload the downloaded `piku-worldcup.json` with the `data` option.

If the script extracts too few items, start the worldcup on PIKU first and run it again on the active match screen. The script scans page state, browser storage, and visible media elements, so it works best after PIKU has loaded the candidate list into the client.

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

## Ollama Check

The bot calls:

```bash
curl http://192.168.0.42:11434/api/generate -d '{
  "model": "SuperQwen-AgentWorld-35B-A3B:Q4",
  "prompt": "Why is the sky blue?",
  "stream": false
}'
```

Make sure the machine running the bot can reach `192.168.0.42:11434`.
