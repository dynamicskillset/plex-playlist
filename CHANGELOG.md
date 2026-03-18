# Changelog

All notable changes to this project will be documented here. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.3.0] — 2026-03-18

### Added
- macOS menu bar app (`menubar.py`) using `rumps` — start, stop, and open the app from the menu bar without managing a terminal
- Menu bar icon reflects container state (`♩` stopped, `♫` running) and auto-refreshes every 10 seconds
- Retry loop waits for Docker Desktop daemon to be ready before starting the container, so the app can be added as a Login Item alongside Docker Desktop without race conditions

### Fixed
- Responsive layout: dashboard playlist table now scrolls horizontally on narrow windows rather than clipping the action buttons
- Create playlist form stacks to a single column on viewports narrower than 600px
- Track list and refresh history tables on the detail page also scroll horizontally

## [0.2.0] — 2026-03-17

### Fixed
- Zero-track generation bug: `_validate_suggestions` was not adding matched IDs to `already_validated_ids`, so intra-response duplicates were not caught. The outer loop then double-checked `validated_ids` (already mutated by `_run_batch`), causing all tracks to be silently dropped. Fixed by adding to `already_validated_ids` immediately after each match and using `validated.extend()` in the outer loop.

### Added
- Regression tests for the full generation pipeline covering the deduplication fix

### Changed
- Licence updated from MIT to AGPL 3.0
- Internal planning and notes documents removed from the repository via `.gitignore`

## [0.1.0] — 2026-03-17

### Added
- Initial implementation of Plex Grounded Playlist Generator
- Grounded playlist generation: every track verified against the Plex library before being written
- Situation-based prompts with configurable LLM (any OpenAI-compatible API)
- Batched LLM calls for large playlists (50–200+ tracks)
- Exact and fuzzy matching (Levenshtein ≥ 85%) to handle metadata inconsistencies
- Sonic Analysis support: mood and BPM annotations included in LLM context when available
- Auto-refresh: detects new library additions and appends matching tracks to existing playlists
- Generation reports with match rates, rejection reasons, and LLM pass counts
- Real-time progress via SSE
- Web UI built with FastAPI, Jinja2, and Pico CSS
- Docker deployment with `docker-compose.yml`
