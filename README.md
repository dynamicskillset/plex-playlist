# Plex Grounded Playlist Generator

A self-hosted web tool that generates large, situation-aware Plex music playlists using a configurable LLM — grounded entirely in your actual library. No phantom tracks.

Plexamp's built-in AI playlist generation regularly suggests tracks that aren't in your library, because the LLM has no grounded knowledge of what you actually own. This tool fixes that by passing your real library to the LLM as context, then validating every suggestion before writing the playlist to Plex.

## Features

- **Grounded generation** — every track is verified against your Plex library before being added
- **Situation-based prompts** — describe a mood or context ("an hour of instrumental music for deep focus") and get a playlist that fits
- **Large playlists** — targets 50+ tracks; batches LLM calls for playlists up to 200+ tracks
- **Backfill retries** — if suggestions don't validate, the tool asks the LLM for replacements automatically
- **Fuzzy matching** — tolerates metadata inconsistencies ("The Beatles" vs "Beatles", minor typos, wrong album attribution)
- **Sonic Analysis support** — if Plex has run Sonic Analysis on your library, mood and BPM data is included in the LLM context
- **Auto-refresh** — detects new library additions and appends matching tracks to existing playlists (append-only, preserves Plexamp playback state)
- **Generation reports** — every playlist shows match rates, rejection reasons, and LLM pass counts so you can debug prompt or metadata issues
- **Real-time progress** — SSE streams status updates to the browser during generation

## Requirements

- Plex Media Server with a music library
- An OpenAI-compatible LLM API (OpenAI, Ollama, or similar)
- Docker

## Quick start

```bash
git clone https://github.com/dynamicskillset/plex-playlist.git
cd plex-playlist
cp .env.example .env
# Edit .env to set PLEX_URL to your Plex server
docker compose up -d
```

Open http://localhost:8484 and follow the setup flow to connect Plex and configure your LLM.

## Configuration

All configuration is done through the web UI. On first run, the setup flow collects:

1. **Plex connection** — sign in with your Plex account or paste a token manually
2. **LLM provider** — base URL, API key, model name, context window size, and temperature

Settings can be changed at any time via the Settings page.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `PLEX_URL` | `http://host.docker.internal:32400` | URL of your Plex Media Server |

If Plex is on a separate machine on your network, set `PLEX_URL` to its IP address, e.g. `http://192.168.0.2:32400`.

## LLM compatibility

Any OpenAI-compatible API works:

- **OpenAI** — set base URL to `https://api.openai.com/v1`
- **Ollama** — set base URL to `http://host.docker.internal:11434/v1` (or your Ollama host)
- Other compatible providers — use their OpenAI-compatible endpoint

## How it works

1. At startup (and after library changes), the tool builds an in-memory index of every artist, album, and track in your Plex music library.
2. When you create a playlist, the full artist/album list (with optional Sonic Analysis annotations) is passed to the LLM as context alongside your prompt.
3. The LLM returns track suggestions as JSON. Each suggestion is validated against the library index using exact and fuzzy matching (Levenshtein ≥ 85%).
4. Suggestions that don't match are discarded. If the playlist falls short of the target count, the tool runs backfill passes (up to 3 per batch) requesting replacements.
5. Once validation is complete, the playlist is written to Plex via its API and appears in Plexamp immediately.

## Stack

- Python 3.12, FastAPI, Uvicorn
- PlexAPI
- httpx (LLM API calls)
- aiosqlite (SQLite)
- rapidfuzz (fuzzy matching)
- Jinja2 + Pico CSS + vanilla JS

## Licence

MIT
