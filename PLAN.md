# Plan

> Last updated: 2026-03-17
> Status: Not started

## Objective

Build the Plex Grounded Playlist Generator as specified in `plex-grounded-playlist-generator-prd.md`. A self-hosted FastAPI web app that generates verified Plex playlists using LLM suggestions grounded in the actual library. Deploys via Docker Compose on an HP Microserver.

## Approach

Build in layers, bottom-up:
1. Project scaffold (Docker, deps, app skeleton)
2. Data layer (SQLite schema, aiosqlite helpers)
3. Plex integration (library index cache)
4. LLM integration (OpenAI-compatible, context building)
5. Matching engine (normalisation + fuzzy match)
6. Generation pipeline (batching, backfill, validation)
7. Job queue + scheduler (worker, polling, debounce)
8. Web UI (templates, SSE, 4 screens)
9. First-run setup flow
10. Integration and edge cases

## Tasks

- [x] Phase 1: Scaffold — Dockerfile, docker-compose.yml, requirements.txt, app skeleton, .env.example
- [x] Phase 2: Data layer — SQLite schema (playlists, generation_reports, refresh_log, config), db.py helpers
- [x] Phase 3: Plex integration — plex.py, library index cache (artists/albums/tracks), Sonic Analysis detection
- [x] Phase 4: LLM integration — llm.py, OpenAI-compatible httpx calls, provider abstraction, context building, token estimation
- [x] Phase 5: Matching engine — matching.py, normalisation, exact match, fuzzy match, artist-only fallback
- [x] Phase 6: Generation pipeline — batching (>60 tracks), backfill retries (3 per batch), LLM response parsing, playlist write to Plex
- [x] Phase 7: Job queue + scheduler — worker.py, asyncio.Queue, scheduler.py, polling, debounce, integrity audit
- [x] Phase 8: Web UI — 4 templates (setup, dashboard, detail, settings), SSE progress, vanilla JS
- [x] Phase 9: First-run setup — Plex sign-in flow, LLM config validation, redirect logic
- [ ] Phase 10: Tests — matching.py and llm.py parsing unit tests (critical reliability paths)
- [ ] Phase 11: End-to-end test — docker compose build, first-run setup, create a playlist
- [ ] Phase 12: Cost tracking display — weekly/monthly LLM call count and estimated cost in Settings

## Decisions Made

| Decision | Rationale | Date |
|----------|-----------|------|
| FastAPI + aiosqlite + httpx | Specified in PRD; async-native, lightweight | 2026-03-17 |
| OpenAI-compatible API only (v1) | Covers OpenAI + Ollama; native Anthropic is single-file addition later | 2026-03-17 |
| Single asyncio.Queue, sequential job processing | Avoids concurrency bugs entirely for single-user tool | 2026-03-17 |
| Append-only refresh | Preserves Plexamp playback position and play history | 2026-03-17 |
| Pico CSS or Simple.css (classless) | No build step, responsive baseline, fits server-rendered HTML | 2026-03-17 |

## Open Questions

- [ ] Which classless CSS framework — Pico CSS or Simple.css? (Pico is more polished but heavier)
- [ ] Does the HP Microserver run Docker? (assumed yes per PRD)
- [ ] What Plex library size to plan for? (PRD mentions 3000+ artist/album pairs as a large library case)

## Out of Scope (v1)

Per PRD Section 10:
- Multi-user support
- Mobile-native UI
- Time-based scheduled refresh
- Streaming service integration
- Playback controls
- Public release
- Preview/review before writing playlist
- Cross-playlist diversity constraints
- Playlist composition operations
- Prompt diff view
