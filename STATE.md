# State

> Last updated: 2026-03-17

## System State Diagram

```mermaid
stateDiagram-v2
    [*] --> Planning: project started
    Planning --> Scaffold: plan approved ← WE ARE HERE
    Scaffold --> DataLayer: scaffold done
    DataLayer --> PlexIntegration: schema ready
    PlexIntegration --> LLMIntegration: index cache working
    LLMIntegration --> MatchingEngine: LLM calls working
    MatchingEngine --> GenerationPipeline: matching working
    GenerationPipeline --> JobQueue: generation working
    JobQueue --> WebUI: queue + scheduler working
    WebUI --> SetupFlow: templates done
    SetupFlow --> Live: setup flow done
```

## Component Status

| Component | Status | Notes |
|-----------|--------|-------|
| Dockerfile + docker-compose | ⏳ Not started | |
| requirements.txt + app skeleton | ⏳ Not started | |
| SQLite schema (db.py) | ⏳ Not started | 4 tables: playlists, generation_reports, refresh_log, config |
| Plex integration (plex.py) | ⏳ Not started | Library index cache, Sonic Analysis detection |
| LLM integration (llm.py) | ⏳ Not started | OpenAI-compatible, httpx, token estimation |
| Matching engine (matching.py) | ⏳ Not started | Normalise, exact, fuzzy (≥85%), artist fallback |
| Generation pipeline | ⏳ Not started | Batching, backfill, LLM response parsing |
| Job queue + scheduler | ⏳ Not started | asyncio.Queue, polling, debounce, integrity audit |
| Web UI templates | ⏳ Not started | 4 screens: setup, dashboard, detail, settings |
| SSE progress streaming | ⏳ Not started | |
| First-run setup flow | ⏳ Not started | Plex sign-in, LLM config validation |

## Data Flow

```mermaid
flowchart LR
    Plex[Plex API] -->|library index| Cache[In-memory index]
    Cache -->|artist/album list| Context[LLM context builder]
    Context -->|grounding + prompt| LLM[LLM API]
    LLM -->|JSON suggestions| Parser[Response parser]
    Parser -->|suggestions| Matcher[Track matcher]
    Matcher -->|validated tracks| Queue[Job queue]
    Queue -->|write playlist| Plex
    Queue -->|store report| DB[(SQLite)]
```

## Dependencies

| Dependency | Status | Notes |
|------------|--------|-------|
| Plex Media Server | Working (external) | Running on HP Microserver, port 32400 |
| LLM API (OpenAI-compatible) | Not configured | Configured at first-run setup |
| Docker | Assumed available | HP Microserver deployment target |
| PlexAPI Python library | Not installed | Goes in requirements.txt |
| rapidfuzz | Not installed | Fuzzy matching |
| aiosqlite | Not installed | Async SQLite |
