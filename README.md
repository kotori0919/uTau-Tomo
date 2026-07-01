# HD Music Bot

A small prefix-command Discord music bot using `discord.py`, `yt-dlp`, and FFmpeg.

## Setup

1. Install Python 3.10 or newer.
2. Install FFmpeg and opus.
3. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`.
5. Enable the Message Content Intent for the bot in the Discord Developer Portal.

## Run

```bash
python bot.py
```

## Commands

- `!play <url or search>` / `!p <url or search>`: add a song to the queue. If nothing is playing, it starts playback.
- `!skip` / `!n`: skip the current song.
- `!queue` / `!pl`: show the current song and queued songs.
- `!stop`: stop playback, clear the queue, and disconnect.
- `!pause`: pause the current song.
- `!resume`: resume a paused song.
- `!volume <0-200>`: set playback volume.

## Notes

Each Discord server gets its own queue and playback state. Song metadata is extracted once when adding to the queue, so `!queue` does not call `yt-dlp` again for every item.
