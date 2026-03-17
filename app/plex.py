"""Plex API integration — library index cache and playlist operations."""
import asyncio
import logging
from typing import Optional

import httpx
from plexapi.server import PlexServer
from plexapi.exceptions import Unauthorized, NotFound

from .matching import LibraryIndex, normalise

logger = logging.getLogger(__name__)

PLEX_SIGNIN_URL = "https://plex.tv/users/sign_in.json"
PLEX_CLIENT_HEADERS = {
    "X-Plex-Client-Identifier": "plex-playlist-generator",
    "X-Plex-Product": "Plex Playlist Generator",
    "X-Plex-Version": "1.0",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


async def acquire_token(username: str, password: str) -> str:
    """Exchange Plex credentials for an auth token. Credentials are not stored."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            PLEX_SIGNIN_URL,
            headers=PLEX_CLIENT_HEADERS,
            json={"user": {"login": username, "password": password}},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["user"]["authToken"]


def connect(plex_url: str, token: str) -> PlexServer:
    """Return a connected PlexServer, raising on failure."""
    return PlexServer(plex_url, token, timeout=30)


def build_library_index(server: PlexServer) -> LibraryIndex:
    """Build a full in-memory index from the Plex music library."""
    index = LibraryIndex()
    music_sections = [s for s in server.library.sections() if s.type == "artist"]
    for section in music_sections:
        for artist in section.all():
            for album in artist.albums():
                for track in album.tracks():
                    index.add_track(
                        artist=artist.title,
                        album=album.title,
                        track=track.title,
                        track_id=str(track.ratingKey),
                    )
    logger.info(
        "Library index built: %d artists, %d tracks",
        index.artist_count,
        index.track_count,
    )
    return index


def get_sonic_data(server: PlexServer, index: LibraryIndex) -> dict | None:
    """Retrieve Sonic Analysis data if available.

    Returns dict[norm_artist][album_name] = annotation_str, or None if unavailable.
    """
    music_sections = [s for s in server.library.sections() if s.type == "artist"]
    if not music_sections:
        return None

    # Sample a few tracks to check if Sonic Analysis fields are populated
    sample_section = music_sections[0]
    sample_tracks = sample_section.searchTracks(limit=5)
    has_sonic = any(
        getattr(t, "musicAnalysisVersion", None) or getattr(t, "loudnessAnalysisVersion", None)
        for t in sample_tracks
    )
    if not has_sonic:
        return None

    logger.info("Sonic Analysis data detected — building album-level annotations")
    sonic: dict[str, dict[str, dict]] = {}

    for section in music_sections:
        for artist in section.all():
            norm_a = normalise(artist.title)
            sonic.setdefault(norm_a, {})
            for album in artist.albums():
                moods: dict[str, int] = {}
                genres: set[str] = set()
                bpms: list[float] = []
                for track in album.tracks():
                    for mood in getattr(track, "moods", []) or []:
                        tag = mood.tag if hasattr(mood, "tag") else str(mood)
                        moods[tag] = moods.get(tag, 0) + 1
                    for genre in getattr(track, "genres", []) or []:
                        tag = genre.tag if hasattr(genre, "tag") else str(genre)
                        genres.add(tag)
                    bpm = getattr(track, "bpm", None)
                    if bpm:
                        bpms.append(float(bpm))

                parts = []
                if moods:
                    top_moods = sorted(moods, key=moods.get, reverse=True)[:3]
                    parts.append("Moods: " + ", ".join(top_moods))
                if genres:
                    parts.append("Genres: " + ", ".join(sorted(genres)[:3]))
                if bpms:
                    parts.append(f"BPM: {int(min(bpms))}-{int(max(bpms))}")

                if parts:
                    sonic[norm_a][album.title] = "; ".join(parts)

    return sonic if sonic else None


def get_library_updated_at(server: PlexServer) -> Optional[int]:
    """Return the max updatedAt timestamp across music sections."""
    music_sections = [s for s in server.library.sections() if s.type == "artist"]
    if not music_sections:
        return None
    return max(
        int(s.updatedAt.timestamp()) if hasattr(s.updatedAt, "timestamp") else 0
        for s in music_sections
    )


def create_playlist(server: PlexServer, name: str, track_ids: list[str]) -> str:
    """Create a playlist on Plex and return its ratingKey."""
    music_sections = [s for s in server.library.sections() if s.type == "artist"]
    if not music_sections:
        raise RuntimeError("No music library found in Plex")
    section = music_sections[0]
    items = [server.fetchItem(int(tid)) for tid in track_ids]
    playlist = server.createPlaylist(name, section=section, items=items)
    return str(playlist.ratingKey)


def update_playlist_tracks(server: PlexServer, playlist_id: str, track_ids: list[str]) -> None:
    """Replace all tracks in an existing Plex playlist."""
    playlist = server.fetchItem(int(playlist_id))
    playlist.removeItems(playlist.items())
    items = [server.fetchItem(int(tid)) for tid in track_ids]
    playlist.addItems(items)


def append_playlist_tracks(server: PlexServer, playlist_id: str, track_ids: list[str]) -> None:
    """Append tracks to an existing Plex playlist."""
    playlist = server.fetchItem(int(playlist_id))
    items = [server.fetchItem(int(tid)) for tid in track_ids]
    playlist.addItems(items)


def get_playlist_track_ids(server: PlexServer, playlist_id: str) -> list[str]:
    """Return current track IDs for a Plex playlist."""
    try:
        playlist = server.fetchItem(int(playlist_id))
        return [str(t.ratingKey) for t in playlist.items()]
    except NotFound:
        return []


def delete_playlist(server: PlexServer, playlist_id: str) -> None:
    """Delete a Plex playlist."""
    try:
        playlist = server.fetchItem(int(playlist_id))
        playlist.delete()
    except NotFound:
        pass


def playlist_exists(server: PlexServer, playlist_id: str) -> bool:
    """Check whether a Plex playlist still exists."""
    try:
        server.fetchItem(int(playlist_id))
        return True
    except NotFound:
        return False
