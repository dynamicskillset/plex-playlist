"""SQLite database layer via aiosqlite."""
import json
import aiosqlite
from pathlib import Path

DB_PATH = Path("/app/data/playlists.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        await db.commit()


SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id                  INTEGER PRIMARY KEY,
    plex_playlist_id    TEXT,
    name                TEXT NOT NULL,
    prompt              TEXT NOT NULL,
    target_track_count  INTEGER NOT NULL DEFAULT 50,
    actual_track_count  INTEGER NOT NULL DEFAULT 0,
    auto_refresh        BOOLEAN NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'generating',
    failure_reason      TEXT,
    used_sonic_analysis BOOLEAN NOT NULL DEFAULT 0,
    created_at          DATETIME NOT NULL DEFAULT (datetime('now')),
    last_refreshed_at   DATETIME
);

CREATE TABLE IF NOT EXISTS generation_reports (
    id                          INTEGER PRIMARY KEY,
    playlist_id                 INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    triggered_by                TEXT NOT NULL,
    llm_passes                  INTEGER NOT NULL DEFAULT 0,
    tracks_suggested            INTEGER NOT NULL DEFAULT 0,
    tracks_validated            INTEGER NOT NULL DEFAULT 0,
    match_exact                 INTEGER NOT NULL DEFAULT 0,
    match_fuzzy                 INTEGER NOT NULL DEFAULT 0,
    match_artist_fallback       INTEGER NOT NULL DEFAULT 0,
    rejected_artist_not_found   INTEGER NOT NULL DEFAULT 0,
    rejected_album_not_found    INTEGER NOT NULL DEFAULT 0,
    rejected_track_not_found    INTEGER NOT NULL DEFAULT 0,
    rejected_unparseable        INTEGER NOT NULL DEFAULT 0,
    used_sonic_analysis         BOOLEAN NOT NULL DEFAULT 0,
    new_library_items           INTEGER,
    new_tracks_added            INTEGER,
    completed_at                DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refresh_log (
    id              INTEGER PRIMARY KEY,
    playlist_id     INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    triggered_by    TEXT NOT NULL,
    tracks_added    INTEGER NOT NULL DEFAULT 0,
    tracks_removed  INTEGER NOT NULL DEFAULT 0,
    tracks_total    INTEGER NOT NULL DEFAULT 0,
    llm_passes      INTEGER NOT NULL DEFAULT 0,
    completed_at    DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS config (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""


async def config_get(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                return row["value"]


async def config_set(key: str, value) -> None:
    encoded = json.dumps(value)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, encoded),
        )
        await db.commit()


async def config_get_all() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM config") as cur:
            rows = await cur.fetchall()
        result = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                result[row["key"]] = row["value"]
        return result
