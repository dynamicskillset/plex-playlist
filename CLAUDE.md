# Project: Plex Grounded Playlist Generator

A self-hosted web tool that generates large, situation-aware Plex playlists using a configurable LLM, grounded entirely in the user's actual Plex music library. Playlists are written directly to Plex via its API and auto-refresh when new music is added.

## Architecture

- `app/` — FastAPI application
  - `main.py` — App entry point, routes, SSE endpoints
  - `worker.py` — Background job worker (asyncio.Queue, sequential processing)
  - `db.py` — SQLite via aiosqlite, schema, migrations
  - `plex.py` — PlexAPI wrapper, library index cache
  - `llm.py` — LLM provider abstraction, OpenAI-compatible calls via httpx
  - `matching.py` — Track normalisation and fuzzy matching (rapidfuzz)
  - `scheduler.py` — Library polling, debounce, auto-refresh trigger
  - `templates/` — Jinja2 HTML templates (setup, dashboard, detail, settings)
  - `static/` — CSS (Pico CSS or Simple.css), minimal vanilla JS
- `data/` — SQLite database (persisted via Docker volume)
- `docker-compose.yml` — Deployment config
- `Dockerfile`
- `.env.example` — Template for environment variables

## Commands

- `docker compose up --build` — build and start
- `docker compose up` — start (no rebuild)
- `docker compose logs -f` — tail logs
- `python3 -m pytest` — run all tests
- `python3 -m pytest tests/test_matching.py tests/test_llm_parsing.py -v` — run critical path tests

## Stack

- Python 3.12+, FastAPI, Uvicorn
- PlexAPI (Plex library interaction)
- httpx (LLM API calls, OpenAI-compatible)
- aiosqlite (async SQLite)
- rapidfuzz (fuzzy matching)
- Jinja2 (server-rendered templates)
- Vanilla JS + EventSource API (SSE progress)
- Pico CSS or Simple.css (classless, no build step)

## Key Concepts

- **Grounded generation:** Library index is built at startup and passed to the LLM as context. Every suggestion is validated against the index before being written to Plex.
- **Batching:** Playlists >60 tracks are built in passes of ≤60. Backfill retries (3 per batch) fill shortfalls.
- **Fuzzy matching:** Levenshtein ≥85% similarity on artist+album+track. Artist-only fallback catches wrong-album suggestions.
- **Auto-refresh:** Library polling every 15 min (configurable), 5-min debounce, append-only merge.
- **Job queue:** Single asyncio.Queue, one job at a time — no concurrency bugs.
- **SSE progress:** FastAPI streams events; browser renders via EventSource.

## Standards

- Single-user tool — no auth, no multi-tenant complexity
- No JS framework, no CSS build step
- All long-running work goes through the job queue
- Plex playlist is only written/overwritten once generation completes successfully
- Tests for matching logic and LLM response parsing (the critical reliability paths)

## Verification

- `docker compose build` after structural changes
- Test matching.py and llm.py parsing logic directly
- Check SSE stream works in browser after UI changes

## Working Rules

- Always check for existing patterns before creating new ones
- Prefer small, incremental changes over big rewrites
- If a task will take more than ~50 lines of changes, use plan mode first
- Don't add dependencies without asking
- Don't refactor code that wasn't part of the task
- Don't create files without explaining what and why

## State & Progress

> Updated: 2026-03-17
> Current focus: Initial project setup — scaffolding structure
> Status: Not started

See PLAN.md for task tracking, STATE.md for system state, HANDOFF.md for session notes.

## Known Issues

- None yet

## Lessons Learned

Things Claude has got wrong on this project — don't repeat these:

- (none yet)
