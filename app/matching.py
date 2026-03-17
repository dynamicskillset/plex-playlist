"""Track normalisation and fuzzy matching against the Plex library index."""
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from rapidfuzz import fuzz


FUZZY_THRESHOLD = 85.0  # minimum similarity % to accept a match


class MatchType(str, Enum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    ARTIST_FALLBACK = "artist_fallback"
    NO_MATCH = "no_match"


@dataclass
class MatchResult:
    match_type: MatchType
    plex_track_id: Optional[str] = None
    rejection_reason: Optional[str] = None  # artist_not_found | album_not_found | track_not_found | unparseable


def normalise(s: str) -> str:
    """Normalise a string for comparison.

    Steps: NFD unicode → ASCII where possible, lowercase, strip,
    remove punctuation (keep hyphens/apostrophes within words),
    collapse spaces.
    """
    if not s:
        return ""
    # Unicode normalisation — replace accented chars with ASCII equivalents
    nfd = unicodedata.normalize("NFD", s)
    ascii_approx = "".join(
        c for c in nfd if unicodedata.category(c) != "Mn"
    )
    lower = ascii_approx.lower().strip()
    # Remove punctuation except hyphens/apostrophes within words
    # Keep: a-z 0-9, hyphen between word chars, apostrophe between word chars
    cleaned = re.sub(r"(?<!\w)['\-]|['\-](?!\w)", " ", lower)
    cleaned = re.sub(r"[^\w\s\-']", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def similarity(a: str, b: str) -> float:
    """Return best similarity score 0–100.

    Uses the max of token_sort_ratio and partial_ratio so that
    substrings like "beatles" still match "the beatles".
    """
    return max(fuzz.token_sort_ratio(a, b), fuzz.partial_ratio(a, b))


def _meets_threshold(a: str, b: str) -> bool:
    return similarity(a, b) >= FUZZY_THRESHOLD


def match_track(
    suggestion: dict,
    index: "LibraryIndex",
) -> MatchResult:
    """Match a single LLM suggestion against the library index.

    suggestion: {"artist": str, "album": str, "track": str}
    Returns a MatchResult.
    """
    artist_raw = suggestion.get("artist", "")
    album_raw = suggestion.get("album", "")
    track_raw = suggestion.get("track", "")

    if not artist_raw or not track_raw:
        return MatchResult(MatchType.NO_MATCH, rejection_reason="unparseable")

    norm_artist = normalise(artist_raw)
    norm_album = normalise(album_raw)
    norm_track = normalise(track_raw)

    # --- Step 1: find matching artist ---
    matched_artists = index.find_artists(norm_artist)
    if not matched_artists:
        return MatchResult(MatchType.NO_MATCH, rejection_reason="artist_not_found")

    # --- Step 2: exact match (artist + album + track) ---
    for artist_key in matched_artists:
        for album_key, tracks in index.albums(artist_key).items():
            if normalise(album_key) == norm_album:
                for track_id, track_norm in tracks.items():
                    if track_norm == norm_track:
                        return MatchResult(MatchType.EXACT, plex_track_id=track_id)

    # --- Step 3: fuzzy match (artist + album + track, all ≥85%) ---
    for artist_key in matched_artists:
        for album_key, tracks in index.albums(artist_key).items():
            if _meets_threshold(normalise(album_key), norm_album):
                for track_id, track_norm in tracks.items():
                    if _meets_threshold(track_norm, norm_track):
                        return MatchResult(MatchType.FUZZY, plex_track_id=track_id)

    # --- Step 4: artist-only fallback (track exists anywhere in artist catalogue) ---
    for artist_key in matched_artists:
        for album_key, tracks in index.albums(artist_key).items():
            for track_id, track_norm in tracks.items():
                if _meets_threshold(track_norm, norm_track):
                    return MatchResult(MatchType.ARTIST_FALLBACK, plex_track_id=track_id)

    # Album was found (artist matched) but track wasn't
    # Check if album at least matched to give a better rejection reason
    album_found = False
    for artist_key in matched_artists:
        for album_key in index.albums(artist_key):
            if _meets_threshold(normalise(album_key), norm_album):
                album_found = True
                break

    if album_found:
        return MatchResult(MatchType.NO_MATCH, rejection_reason="track_not_found")
    return MatchResult(MatchType.NO_MATCH, rejection_reason="album_not_found")


class LibraryIndex:
    """In-memory index of the Plex library for fast validation.

    Structure:
        _artists: dict[norm_artist_key, original_name]
        _albums:  dict[norm_artist_key, dict[original_album_name, dict[plex_track_id, norm_track_name]]]
    """

    def __init__(self):
        self._artists: dict[str, str] = {}
        self._albums: dict[str, dict[str, dict[str, str]]] = {}

    def add_track(self, artist: str, album: str, track: str, track_id: str) -> None:
        norm_a = normalise(artist)
        if norm_a not in self._artists:
            self._artists[norm_a] = artist
            self._albums[norm_a] = {}
        if album not in self._albums[norm_a]:
            self._albums[norm_a][album] = {}
        self._albums[norm_a][album][track_id] = normalise(track)

    def find_artists(self, norm_artist: str) -> list[str]:
        """Return list of artist keys that match (exact or fuzzy)."""
        # Exact first
        if norm_artist in self._artists:
            return [norm_artist]
        # Fuzzy
        return [
            key for key in self._artists
            if _meets_threshold(key, norm_artist)
        ]

    def albums(self, artist_key: str) -> dict[str, dict[str, str]]:
        return self._albums.get(artist_key, {})

    @property
    def artist_count(self) -> int:
        return len(self._artists)

    @property
    def track_count(self) -> int:
        return sum(
            len(tracks)
            for albums in self._albums.values()
            for tracks in albums.values()
        )

    def artist_album_list(self, sonic_data: dict | None = None) -> list[str]:
        """Return a list of 'Artist — Album' strings for LLM context.

        If sonic_data is provided, appends mood/BPM annotations.
        sonic_data: dict[norm_artist_key][album_name] = annotation_string
        """
        lines = []
        for norm_a, artist_name in sorted(self._artists.items()):
            for album_name in sorted(self._albums[norm_a].keys()):
                annotation = ""
                if sonic_data:
                    annotation = sonic_data.get(norm_a, {}).get(album_name, "")
                if annotation:
                    lines.append(f'"{album_name}" by {artist_name} — {annotation}')
                else:
                    lines.append(f'"{album_name}" by {artist_name}')
        return lines
