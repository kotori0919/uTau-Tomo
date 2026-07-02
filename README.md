# HD Music Bot

A Discord slash-command music bot with local Gemma/Ollama chat memory, voice-channel conversation, and CPU Supertonic 3 TTS.

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

# Ollama / Gemma
OLLAMA_URL=http://192.168.0.42:11434
OLLAMA_MODEL=gemma4-E4B-Unhold:latest
OLLAMA_TIMEOUT=180

# Web search for AI answers
WEB_SEARCH_ENABLED=true
WEB_SEARCH_MAX_RESULTS=5

# Local data and memory
BOT_DATA_DIR=data
MEMORY_DB_PATH=data/memory.sqlite3

# Supertonic 3 TTS
SUPERTONIC_DEFAULT_VOICE=M4
SUPERTONIC_DEFAULT_LANG=ko
SUPERTONIC_MAX_CHARS=700

# Optional: command that converts reference audio into a Supertonic 3 voice JSON.
# Use {input} and {output}; the command is only invoked by the admin-only /voice_refer command.
# SUPERTONIC_VOICE_BUILDER_CMD=/path/to/build_voice --input {input} --output {output}
```

## Slash Commands

- `/play query`: add a song to the queue. If nothing is playing, it starts playback.
- `/skip`: skip the current song.
- `/queue`: show the current song and queued songs.
- `/stop`: stop playback, clear the queue, and disconnect.
- `/pause`: pause the current song.
- `/resume`: resume a paused song.
- `/volume volume`: set playback volume from 0 to 200.
- `/context_reset`: reset your personal AI context and long-term memory in this server.
- `/server_context_reset`: reset shared server context. Administrator only.
- `/voice_join always_reply`: join your voice channel, listen, answer with Gemma, and speak using Supertonic TTS.
- `/voice_leave`: stop voice conversation and leave.
- `/voice_style voice_name lang`: set built-in Supertonic voice and language. Administrator only.
- `/voice_refer refer`: apply a Supertonic voice style JSON, or run an optional reference-audio-to-JSON converter. Administrator only.

## Mention Chat

Mention the bot in any server text channel to talk to Gemma through Ollama. The bot keeps:

- per-user recent context;
- per-user long-term memory;
- shared server context from public messages;
- automatic compression into SQLite summaries.

Example:

```text
@HD Music Bot 오늘 대화 이어서 생각해줘
```

Audio/image attachments on a mention are passed to Ollama as multimodal inputs when the model/runtime supports them.

When the model decides that current external information is needed, it can request a web search. The bot runs the search with `ddgs`, injects the results back into Gemma, and asks it to answer with source URLs.

## Voice And TTS Notes

Voice receiving uses `discord-ext-voice-recv`, because `discord.py` itself only sends audio. Supertonic 3 runs locally on CPU through the `supertonic` Python SDK and downloads its model assets on first use.

The open Supertonic ONNX SDK can load built-in voices and Supertonic voice-style JSON files. It does not ship a local reference-audio-to-voice-style converter. For voice cloning, upload a Voice Builder Supertonic 3 JSON with `/voice_refer`, or configure `SUPERTONIC_VOICE_BUILDER_CMD` if you have a local converter that produces that JSON from an audio reference.

If music or another audio source is already playing, the bot sends the AI answer as text and skips TTS playback so it does not destroy the music stream.

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
  "model": "gemma4-E4B-Unhold:latest",
  "prompt": "Why is the sky blue?"
}'
```

Make sure the machine running the bot can reach that URL.
