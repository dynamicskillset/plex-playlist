"""FastAPI application — routes, startup, job handlers, SSE."""
import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from plexapi.exceptions import Unauthorized

from .db import DB_PATH, config_get, config_get_all, config_set, init_db
from .generator import generate_playlist
from .llm import LLMConfig, default_context_window, validate_llm_connection
from .matching import LibraryIndex
from .plex import (
    acquire_token, append_playlist_tracks, build_library_index, connect,
    create_playlist, delete_playlist, get_library_updated_at,
    get_playlist_track_ids, get_sonic_data, playlist_exists,
    update_playlist_tracks,
)
from .scheduler import LibraryScheduler
from .worker import Job, JobQueue, JobType, queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ── App state ──────────────────────────────────────────────────────────────────

class AppState:
    plex_server = None
    library_index: LibraryIndex | None = None
    sonic_data: dict | None = None
    scheduler: LibraryScheduler | None = None
    worker_task: asyncio.Task | None = None
    scheduler_task: asyncio.Task | None = None

state = AppState()

# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    state.worker_task = asyncio.create_task(queue.run(_job_handlers()))
    # Connect to Plex and build index in background — don't block startup
    asyncio.create_task(_try_connect_plex())
    yield
    if state.worker_task:
        state.worker_task.cancel()
    if state.scheduler_task:
        state.scheduler_task.cancel()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=str(os.path.join(os.path.dirname(__file__), "templates")))
app.mount("/static", StaticFiles(directory=str(os.path.join(os.path.dirname(__file__), "static"))), name="static")


# ── Setup helpers ──────────────────────────────────────────────────────────────

async def _try_connect_plex() -> None:
    token = await config_get("plex_token")
    plex_url = os.getenv("PLEX_URL", "http://localhost:32400")
    if not token:
        return
    try:
        loop = asyncio.get_event_loop()
        state.plex_server = connect(plex_url, token)
        logger.info("Connected to Plex at %s — building library index in background", plex_url)
        state.library_index = await loop.run_in_executor(None, build_library_index, state.plex_server)
        state.sonic_data = await loop.run_in_executor(None, get_sonic_data, state.plex_server, state.library_index)
        logger.info("Library index ready: %d tracks", state.library_index.track_count)
        if not state.scheduler_task:
            _start_scheduler()
    except Exception as e:
        logger.warning("Could not connect to Plex: %s", e)
        state.plex_server = None


def _start_scheduler() -> None:
    async def on_change(updated_at: int):
        cfg = await config_get_all()
        auto_refresh_paused = cfg.get("auto_refresh_paused", False)
        # Rebuild index
        try:
            state.library_index = build_library_index(state.plex_server)
            state.sonic_data = get_sonic_data(state.plex_server, state.library_index)
        except Exception as e:
            logger.error("Failed to rebuild library index: %s", e)
        # Queue refresh cycle (unless globally paused)
        if not auto_refresh_paused:
            queue.enqueue(Job(type=JobType.REFRESH_CYCLE, payload={"new_updated_at": updated_at}))
        # Always run integrity audit
        queue.enqueue(Job(type=JobType.INTEGRITY_AUDIT))

    poll_interval = int(os.getenv("POLL_INTERVAL", "900"))
    debounce_window = int(os.getenv("DEBOUNCE_WINDOW", "300"))

    state.scheduler = LibraryScheduler(
        poll_interval=poll_interval,
        debounce_window=debounce_window,
        on_change=on_change,
        get_updated_at=lambda: get_library_updated_at(state.plex_server) if state.plex_server else None,
    )
    state.scheduler_task = asyncio.create_task(state.scheduler.run())


async def _is_setup_complete() -> bool:
    token = await config_get("plex_token")
    llm_url = await config_get("llm_base_url")
    return bool(token and llm_url)


# ── Job handlers ───────────────────────────────────────────────────────────────

def _job_handlers() -> dict:
    return {
        JobType.CREATE_PLAYLIST: _handle_create,
        JobType.REFRESH_PLAYLIST: _handle_refresh,
        JobType.FULL_REGENERATE: _handle_full_regenerate,
        JobType.PROMPT_EDIT: _handle_prompt_edit,
        JobType.REFRESH_CYCLE: _handle_refresh_cycle,
        JobType.INTEGRITY_AUDIT: _handle_integrity_audit,
    }


async def _get_llm_config() -> LLMConfig | None:
    cfg = await config_get_all()
    base_url = cfg.get("llm_base_url")
    api_key = cfg.get("llm_api_key")
    model = cfg.get("llm_model")
    if not (base_url and api_key and model):
        return None
    context_window = int(cfg.get("llm_context_window") or default_context_window(model))
    temperature = float(cfg.get("llm_temperature", 0.9))
    return LLMConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        context_window=context_window,
        temperature=temperature,
    )


async def _handle_create(job: Job) -> None:
    playlist_id = job.payload["playlist_id"]
    sse_key = job.sse_key

    def emit(msg: str):
        if sse_key:
            queue.emit(sse_key, msg)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return

    if not state.plex_server or not state.library_index:
        await _mark_failed(playlist_id, "Plex server not connected")
        return

    llm_config = await _get_llm_config()
    if not llm_config:
        await _mark_failed(playlist_id, "LLM not configured")
        return

    album_list = state.library_index.artist_album_list(state.sonic_data)

    result = await generate_playlist(
        prompt=row["prompt"],
        target_count=row["target_track_count"],
        index=state.library_index,
        llm_config=llm_config,
        album_list=album_list,
        sonic_data=state.sonic_data,
        is_full_regeneration=False,
        progress=emit,
    )

    if not result.success and len(result.validated_tracks) < 20:
        await _mark_failed(playlist_id, result.error)
        emit(f"Failed — {result.error}")
        return

    emit("Writing playlist to Plex...")
    try:
        track_ids = [t["plex_track_id"] for t in result.validated_tracks]
        plex_id = create_playlist(state.plex_server, row["name"], track_ids)
    except Exception as e:
        await _mark_failed(playlist_id, f"Failed to write playlist to Plex: {e}")
        emit(f"Failed — could not write to Plex: {e}")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE playlists SET plex_playlist_id=?, actual_track_count=?,
               status='ready', used_sonic_analysis=?, last_refreshed_at=datetime('now')
               WHERE id=?""",
            (plex_id, len(result.validated_tracks), result.stats.used_sonic_analysis, playlist_id),
        )
        await db.execute(
            """INSERT INTO generation_reports
               (playlist_id, triggered_by, llm_passes, tracks_suggested, tracks_validated,
                match_exact, match_fuzzy, match_artist_fallback,
                rejected_artist_not_found, rejected_album_not_found,
                rejected_track_not_found, rejected_unparseable, used_sonic_analysis)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (playlist_id, "create", result.stats.llm_passes, result.stats.tracks_suggested,
             result.stats.tracks_validated, result.stats.match_exact, result.stats.match_fuzzy,
             result.stats.match_artist_fallback, result.stats.rejected_artist_not_found,
             result.stats.rejected_album_not_found, result.stats.rejected_track_not_found,
             result.stats.rejected_unparseable, result.stats.used_sonic_analysis),
        )
        await db.commit()

    emit(f"Done — {len(result.validated_tracks)} tracks added")


async def _handle_refresh(job: Job) -> None:
    await _do_refresh(job.payload["playlist_id"], triggered_by="manual_refresh", sse_key=job.sse_key)


async def _handle_full_regenerate(job: Job) -> None:
    await _do_full_regenerate(job.payload["playlist_id"], triggered_by="full_regenerate", sse_key=job.sse_key)


async def _handle_prompt_edit(job: Job) -> None:
    playlist_id = job.payload["playlist_id"]
    new_prompt = job.payload["new_prompt"]
    # Update prompt first
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE playlists SET prompt=? WHERE id=?", (new_prompt, playlist_id))
        await db.commit()
    await _do_full_regenerate(playlist_id, triggered_by="prompt_edit", sse_key=job.sse_key)


async def _handle_refresh_cycle(job: Job) -> None:
    cfg = await config_get_all()
    call_cap = int(cfg.get("max_llm_calls_per_cycle", 10))
    new_updated_at = job.payload.get("new_updated_at")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id FROM playlists WHERE auto_refresh=1 AND status='ready'
               ORDER BY last_refreshed_at ASC NULLS FIRST LIMIT ?""",
            (call_cap,),
        ) as cur:
            rows = await cur.fetchall()

    for row in rows:
        await _do_refresh(row["id"], triggered_by="auto_refresh", new_library_items=new_updated_at)


async def _handle_integrity_audit(_job: Job) -> None:
    if not state.plex_server:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, plex_playlist_id, actual_track_count FROM playlists WHERE status='ready'") as cur:
            rows = await cur.fetchall()

    for row in rows:
        plex_id = row["plex_playlist_id"]
        if not plex_id:
            continue
        if not playlist_exists(state.plex_server, plex_id):
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE playlists SET status='orphaned' WHERE id=?", (row["id"],))
                await db.commit()
            continue
        # Check for removed tracks
        current_ids = set(get_playlist_track_ids(state.plex_server, plex_id))
        valid_ids = {tid for tid in current_ids if state.library_index and tid in _all_track_ids()}
        removed = len(current_ids) - len(valid_ids)
        if removed > 0:
            logger.info("Integrity audit: removing %d phantom tracks from playlist %d", removed, row["id"])
            # Remove phantom tracks from Plex playlist
            valid_items = list(valid_ids)
            update_playlist_tracks(state.plex_server, plex_id, valid_items)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE playlists SET actual_track_count=? WHERE id=?",
                    (len(valid_ids), row["id"]),
                )
                await db.execute(
                    """INSERT INTO refresh_log (playlist_id, triggered_by, tracks_removed, tracks_total)
                       VALUES (?, 'integrity_audit', ?, ?)""",
                    (row["id"], removed, len(valid_ids)),
                )
                await db.commit()


def _all_track_ids() -> set[str]:
    if not state.library_index:
        return set()
    ids = set()
    for albums in state.library_index._albums.values():
        for tracks in albums.values():
            ids.update(tracks.keys())
    return ids


async def _do_refresh(playlist_id: int, triggered_by: str, sse_key: str | None = None, new_library_items=None) -> None:
    if not state.plex_server or not state.library_index:
        return

    def emit(msg: str):
        if sse_key:
            queue.emit(sse_key, msg)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        return

    llm_config = await _get_llm_config()
    if not llm_config:
        return

    # Get existing track IDs to pass as already_validated
    existing_ids = get_playlist_track_ids(state.plex_server, row["plex_playlist_id"])
    existing_validated = [{"plex_track_id": tid, "artist": "", "album": "", "track": ""} for tid in existing_ids]

    album_list = state.library_index.artist_album_list(state.sonic_data)
    result = await generate_playlist(
        prompt=row["prompt"],
        target_count=row["target_track_count"],
        index=state.library_index,
        llm_config=llm_config,
        album_list=album_list,
        sonic_data=state.sonic_data,
        already_validated=existing_validated,
        is_full_regeneration=False,
        progress=emit,
    )

    new_tracks = [t for t in result.validated_tracks if t["plex_track_id"] not in set(existing_ids)]
    if new_tracks:
        emit("Appending new tracks to Plex playlist...")
        append_playlist_tracks(state.plex_server, row["plex_playlist_id"], [t["plex_track_id"] for t in new_tracks])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE playlists SET actual_track_count=?, last_refreshed_at=datetime('now'),
               used_sonic_analysis=? WHERE id=?""",
            (len(existing_ids) + len(new_tracks), result.stats.used_sonic_analysis, playlist_id),
        )
        await db.execute(
            """INSERT INTO refresh_log (playlist_id, triggered_by, tracks_added, tracks_total, llm_passes)
               VALUES (?, ?, ?, ?, ?)""",
            (playlist_id, triggered_by, len(new_tracks),
             len(existing_ids) + len(new_tracks), result.stats.llm_passes),
        )
        await db.execute(
            """INSERT INTO generation_reports
               (playlist_id, triggered_by, llm_passes, tracks_suggested, tracks_validated,
                match_exact, match_fuzzy, match_artist_fallback,
                rejected_artist_not_found, rejected_album_not_found,
                rejected_track_not_found, rejected_unparseable,
                used_sonic_analysis, new_library_items, new_tracks_added)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (playlist_id, triggered_by, result.stats.llm_passes, result.stats.tracks_suggested,
             len(new_tracks), result.stats.match_exact, result.stats.match_fuzzy,
             result.stats.match_artist_fallback, result.stats.rejected_artist_not_found,
             result.stats.rejected_album_not_found, result.stats.rejected_track_not_found,
             result.stats.rejected_unparseable, result.stats.used_sonic_analysis,
             new_library_items, len(new_tracks)),
        )
        await db.commit()

    emit(f"Done — {len(new_tracks)} new tracks added")


async def _do_full_regenerate(playlist_id: int, triggered_by: str, sse_key: str | None = None) -> None:
    if not state.plex_server or not state.library_index:
        return

    def emit(msg: str):
        if sse_key:
            queue.emit(sse_key, msg)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        return

    llm_config = await _get_llm_config()
    if not llm_config:
        return

    album_list = state.library_index.artist_album_list(state.sonic_data)
    result = await generate_playlist(
        prompt=row["prompt"],
        target_count=row["target_track_count"],
        index=state.library_index,
        llm_config=llm_config,
        album_list=album_list,
        sonic_data=state.sonic_data,
        is_full_regeneration=True,
        progress=emit,
    )

    if not result.success and len(result.validated_tracks) < 20:
        # Don't overwrite the existing playlist — preserve it
        await _mark_failed(playlist_id, result.error)
        emit(f"Failed — {result.error}")
        return

    emit("Writing playlist to Plex...")
    track_ids = [t["plex_track_id"] for t in result.validated_tracks]
    update_playlist_tracks(state.plex_server, row["plex_playlist_id"], track_ids)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE playlists SET actual_track_count=?, status='ready',
               used_sonic_analysis=?, last_refreshed_at=datetime('now')
               WHERE id=?""",
            (len(result.validated_tracks), result.stats.used_sonic_analysis, playlist_id),
        )
        await db.execute(
            """INSERT INTO generation_reports
               (playlist_id, triggered_by, llm_passes, tracks_suggested, tracks_validated,
                match_exact, match_fuzzy, match_artist_fallback,
                rejected_artist_not_found, rejected_album_not_found,
                rejected_track_not_found, rejected_unparseable, used_sonic_analysis)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (playlist_id, triggered_by, result.stats.llm_passes, result.stats.tracks_suggested,
             result.stats.tracks_validated, result.stats.match_exact, result.stats.match_fuzzy,
             result.stats.match_artist_fallback, result.stats.rejected_artist_not_found,
             result.stats.rejected_album_not_found, result.stats.rejected_track_not_found,
             result.stats.rejected_unparseable, result.stats.used_sonic_analysis),
        )
        await db.commit()

    emit(f"Done — {len(result.validated_tracks)} tracks")


async def _mark_failed(playlist_id: int, reason: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE playlists SET status='failed', failure_reason=? WHERE id=?",
            (reason, playlist_id),
        )
        await db.commit()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not await _is_setup_complete():
        return RedirectResponse("/setup")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT p.*,
               (SELECT CAST(tracks_validated AS FLOAT) / NULLIF(tracks_suggested, 0) * 100
                FROM generation_reports WHERE playlist_id=p.id ORDER BY completed_at DESC LIMIT 1) as hit_rate,
               (SELECT triggered_by FROM generation_reports WHERE playlist_id=p.id ORDER BY completed_at DESC LIMIT 1) as last_trigger,
               (SELECT new_tracks_added FROM generation_reports WHERE playlist_id=p.id ORDER BY completed_at DESC LIMIT 1) as last_new_tracks
               FROM playlists p ORDER BY created_at DESC"""
        ) as cur:
            playlists = await cur.fetchall()
    cfg = await config_get_all()
    auto_refresh_paused = cfg.get("auto_refresh_paused", False)
    library_size = state.library_index.track_count if state.library_index else 0
    plex_connected = state.plex_server is not None
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "playlists": playlists,
        "auto_refresh_paused": auto_refresh_paused,
        "library_size": library_size,
        "plex_connected": plex_connected,
        "queue_status": queue.status(),
    })


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, step: str = "plex"):
    return templates.TemplateResponse("setup.html", {"request": request, "step": step, "error": None})


@app.post("/setup/plex")
async def setup_plex(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    token: str = Form(default=""),
):
    plex_url = os.getenv("PLEX_URL", "http://localhost:32400")
    error = None
    if token:
        plex_token = token.strip()
    elif username and password:
        try:
            plex_token = await acquire_token(username, password)
        except Exception as e:
            error = f"Sign-in failed: {e}"
            return templates.TemplateResponse("setup.html", {"request": request, "step": "plex", "error": error})
    else:
        error = "Provide either credentials or a token."
        return templates.TemplateResponse("setup.html", {"request": request, "step": "plex", "error": error})

    # Validate connection
    try:
        srv = connect(plex_url, plex_token)
        srv.library.sections()  # test call
    except Unauthorized:
        error = "Token is invalid or not authorised."
        return templates.TemplateResponse("setup.html", {"request": request, "step": "plex", "error": error})
    except Exception as e:
        error = f"Could not connect to Plex at {plex_url}: {e}"
        return templates.TemplateResponse("setup.html", {"request": request, "step": "plex", "error": error})

    await config_set("plex_token", plex_token)
    state.plex_server = srv
    return RedirectResponse("/setup/plex/indexing", status_code=303)


@app.get("/setup/plex/indexing", response_class=HTMLResponse)
async def setup_plex_indexing(request: Request):
    return templates.TemplateResponse("setup_indexing.html", {"request": request})


@app.get("/setup/plex/indexing/progress")
async def setup_plex_indexing_progress():
    async def stream():
        if not state.plex_server:
            yield "data: error: Plex not connected\n\n"
            return
        try:
            yield "data: Connecting to Plex library...\n\n"
            await asyncio.sleep(0)

            loop = asyncio.get_event_loop()
            yield "data: Building library index...\n\n"
            state.library_index = await loop.run_in_executor(
                None, build_library_index, state.plex_server
            )
            track_count = state.library_index.track_count
            artist_count = state.library_index.artist_count
            yield f"data: Indexed {artist_count} artists and {track_count} tracks...\n\n"

            yield "data: Checking for Sonic Analysis data...\n\n"
            state.sonic_data = await loop.run_in_executor(
                None, get_sonic_data, state.plex_server, state.library_index
            )
            if state.sonic_data:
                yield "data: Sonic Analysis data found — mood and BPM annotations loaded.\n\n"
            else:
                yield "data: No Sonic Analysis data available — using standard metadata.\n\n"

            yield "data: done\n\n"
        except Exception as e:
            yield f"data: error: {e}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/setup/llm")
async def setup_llm(
    request: Request,
    base_url: str = Form(),
    api_key: str = Form(),
    model: str = Form(),
    context_window: str = Form(default=""),
    temperature: str = Form(default="0.9"),
):
    ctx_win = int(context_window) if context_window.strip() else default_context_window(model)
    cfg = LLMConfig(
        base_url=base_url.strip(),
        api_key=api_key.strip(),
        model=model.strip(),
        context_window=ctx_win,
        temperature=float(temperature),
    )
    ok = await validate_llm_connection(cfg)
    if not ok:
        return templates.TemplateResponse("setup.html", {
            "request": request,
            "step": "llm",
            "error": "Could not connect to LLM. Check the base URL and API key.",
        })
    await config_set("llm_base_url", cfg.base_url)
    await config_set("llm_api_key", cfg.api_key)
    await config_set("llm_model", cfg.model)
    await config_set("llm_context_window", cfg.context_window)
    await config_set("llm_temperature", cfg.temperature)
    return RedirectResponse("/", status_code=303)


@app.post("/playlists/create")
async def create_playlist_route(
    request: Request,
    prompt: str = Form(),
    name: str = Form(default=""),
    target_count: int = Form(default=50),
):
    if not await _is_setup_complete():
        return RedirectResponse("/setup", status_code=303)
    if not state.library_index:
        raise HTTPException(503, "Plex not connected")

    playlist_name = name.strip() or _name_from_prompt(prompt)
    sse_key = str(uuid.uuid4())

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO playlists (name, prompt, target_track_count, status)
               VALUES (?, ?, ?, 'generating')""",
            (playlist_name, prompt.strip(), target_count),
        )
        playlist_id = cur.lastrowid
        await db.commit()

    queue.enqueue(Job(type=JobType.CREATE_PLAYLIST, payload={"playlist_id": playlist_id}, sse_key=sse_key))
    return RedirectResponse(f"/playlists/{playlist_id}?sse_key={sse_key}", status_code=303)


@app.get("/playlists/{playlist_id}", response_class=HTMLResponse)
async def playlist_detail(request: Request, playlist_id: int, sse_key: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)) as cur:
            playlist = await cur.fetchone()
        if not playlist:
            raise HTTPException(404, "Playlist not found")
        async with db.execute(
            "SELECT * FROM generation_reports WHERE playlist_id=? ORDER BY completed_at DESC",
            (playlist_id,),
        ) as cur:
            reports = await cur.fetchall()
        async with db.execute(
            "SELECT * FROM refresh_log WHERE playlist_id=? ORDER BY completed_at DESC LIMIT 20",
            (playlist_id,),
        ) as cur:
            refresh_history = await cur.fetchall()

    # Fetch track list from Plex
    tracks = []
    if state.plex_server and playlist["plex_playlist_id"]:
        try:
            from plexapi.exceptions import NotFound
            plex_pl = state.plex_server.fetchItem(int(playlist["plex_playlist_id"]))
            tracks = [(t.grandparentTitle, t.parentTitle, t.title) for t in plex_pl.items()]
        except Exception:
            pass

    return templates.TemplateResponse("detail.html", {
        "request": request,
        "playlist": playlist,
        "reports": reports,
        "refresh_history": refresh_history,
        "tracks": tracks,
        "sse_key": sse_key,
    })


@app.get("/playlists/{playlist_id}/progress")
async def playlist_progress(playlist_id: int, sse_key: str):
    return StreamingResponse(
        queue.stream(sse_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/playlists/{playlist_id}/refresh")
async def refresh_playlist(playlist_id: int):
    sse_key = str(uuid.uuid4())
    queue.enqueue(Job(type=JobType.REFRESH_PLAYLIST, payload={"playlist_id": playlist_id}, sse_key=sse_key))
    return RedirectResponse(f"/playlists/{playlist_id}?sse_key={sse_key}", status_code=303)


@app.post("/playlists/{playlist_id}/regenerate")
async def regenerate_playlist(playlist_id: int):
    sse_key = str(uuid.uuid4())
    queue.enqueue(Job(type=JobType.FULL_REGENERATE, payload={"playlist_id": playlist_id}, sse_key=sse_key))
    return RedirectResponse(f"/playlists/{playlist_id}?sse_key={sse_key}", status_code=303)


@app.post("/playlists/{playlist_id}/edit-prompt")
async def edit_prompt(playlist_id: int, prompt: str = Form()):
    sse_key = str(uuid.uuid4())
    queue.enqueue(Job(
        type=JobType.PROMPT_EDIT,
        payload={"playlist_id": playlist_id, "new_prompt": prompt.strip()},
        sse_key=sse_key,
    ))
    return RedirectResponse(f"/playlists/{playlist_id}?sse_key={sse_key}", status_code=303)


@app.post("/playlists/{playlist_id}/toggle-auto-refresh")
async def toggle_auto_refresh(playlist_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE playlists SET auto_refresh = NOT auto_refresh WHERE id=?", (playlist_id,)
        )
        await db.commit()
    return RedirectResponse(f"/playlists/{playlist_id}", status_code=303)


@app.post("/playlists/{playlist_id}/delete")
async def delete_playlist_route(playlist_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT plex_playlist_id FROM playlists WHERE id=?", (playlist_id,)) as cur:
            row = await cur.fetchone()
        if row and row["plex_playlist_id"] and state.plex_server:
            try:
                delete_playlist(state.plex_server, row["plex_playlist_id"])
            except Exception:
                pass
        await db.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        await db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/playlists/{playlist_id}/remove-orphan")
async def remove_orphan(playlist_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM playlists WHERE id=? AND status='orphaned'", (playlist_id,))
        await db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = await config_get_all()
    library_info = {
        "track_count": state.library_index.track_count if state.library_index else 0,
        "artist_count": state.library_index.artist_count if state.library_index else 0,
        "sonic_analysis": state.sonic_data is not None,
    }
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "cfg": cfg,
        "library_info": library_info,
        "plex_connected": state.plex_server is not None,
    })


@app.post("/settings/save")
async def save_settings(
    request: Request,
    llm_base_url: str = Form(default=""),
    llm_api_key: str = Form(default=""),
    llm_model: str = Form(default=""),
    llm_context_window: str = Form(default=""),
    llm_temperature: str = Form(default="0.9"),
    llm_cost_per_call: str = Form(default="0.01"),
    poll_interval: str = Form(default="900"),
    debounce_window: str = Form(default="300"),
    max_llm_calls_per_cycle: str = Form(default="10"),
    auto_refresh_paused: str = Form(default=""),
):
    updates = {
        "llm_base_url": llm_base_url.strip(),
        "llm_api_key": llm_api_key.strip(),
        "llm_model": llm_model.strip(),
        "llm_temperature": float(llm_temperature),
        "llm_cost_per_call": float(llm_cost_per_call),
        "poll_interval": int(poll_interval),
        "debounce_window": int(debounce_window),
        "max_llm_calls_per_cycle": int(max_llm_calls_per_cycle),
        "auto_refresh_paused": bool(auto_refresh_paused),
    }
    if llm_context_window.strip():
        updates["llm_context_window"] = int(llm_context_window)
    for k, v in updates.items():
        await config_set(k, v)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/reconnect-plex")
async def reconnect_plex(request: Request, token: str = Form(default="")):
    plex_url = os.getenv("PLEX_URL", "http://localhost:32400")
    if token.strip():
        await config_set("plex_token", token.strip())
    await _try_connect_plex()
    return RedirectResponse("/settings", status_code=303)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _name_from_prompt(prompt: str) -> str:
    words = prompt.strip().split()[:6]
    name = " ".join(words)
    if len(prompt.split()) > 6:
        name += "..."
    return name.capitalize()
