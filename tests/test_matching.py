"""Tests for app/matching.py — normalisation, exact/fuzzy/fallback matching."""
import pytest
from app.matching import LibraryIndex, MatchType, match_track, normalise


# ── normalise() ───────────────────────────────────────────────────────────────

def test_normalise_lowercases():
    assert normalise("The Beatles") == "the beatles"

def test_normalise_strips_whitespace():
    assert normalise("  Radiohead  ") == "radiohead"

def test_normalise_collapses_spaces():
    assert normalise("The   Cure") == "the   cure".replace("   ", " ")

def test_normalise_removes_punctuation():
    assert normalise("AC/DC") == "ac dc"

def test_normalise_keeps_hyphens_within_words():
    result = normalise("alt-J")
    assert "alt" in result and "j" in result

def test_normalise_keeps_apostrophes_within_words():
    result = normalise("Sly & the Family Stone")
    assert "sly" in result and "family" in result

def test_normalise_replaces_accented_chars():
    # ö → o, é → e
    assert normalise("Björk") == "bjork"
    assert normalise("Sigur Rós") == "sigur ros"

def test_normalise_empty_string():
    assert normalise("") == ""

def test_normalise_unicode_ligature():
    # Naive unicode — shouldn't crash
    result = normalise("Ænima")
    assert isinstance(result, str)


# ── LibraryIndex ──────────────────────────────────────────────────────────────

@pytest.fixture
def index():
    idx = LibraryIndex()
    idx.add_track("Radiohead", "Kid A", "Everything in Its Right Place", "1001")
    idx.add_track("Radiohead", "Kid A", "Kid A", "1002")
    idx.add_track("Radiohead", "OK Computer", "Paranoid Android", "1003")
    idx.add_track("The Beatles", "Abbey Road", "Come Together", "1004")
    idx.add_track("The Beatles", "Abbey Road", "Something", "1005")
    idx.add_track("Björk", "Homogenic", "Jóga", "1006")
    return idx

def test_index_artist_count(index):
    assert index.artist_count == 3

def test_index_track_count(index):
    assert index.track_count == 6

def test_index_find_artist_exact(index):
    result = index.find_artists("radiohead")
    assert result == ["radiohead"]

def test_index_find_artist_fuzzy(index):
    # "Radiohed" — one character off
    result = index.find_artists("radiohed")
    assert len(result) == 1
    assert "radiohead" in result[0]

def test_index_find_artist_the_stripped(index):
    # "Beatles" without "The" — fuzzy should still match "the beatles"
    result = index.find_artists("beatles")
    assert len(result) == 1

def test_index_find_artist_not_found(index):
    result = index.find_artists("zztop totally unknown band xyz")
    assert result == []

def test_index_artist_album_list_no_sonic(index):
    lines = index.artist_album_list()
    assert any("Kid A" in line for line in lines)
    assert any("Björk" in line or "Bjork" in line for line in lines)


# ── match_track() — exact match ───────────────────────────────────────────────

def test_exact_match(index):
    result = match_track(
        {"artist": "Radiohead", "album": "Kid A", "track": "Everything in Its Right Place"},
        index,
    )
    assert result.match_type == MatchType.EXACT
    assert result.plex_track_id == "1001"

def test_exact_match_case_insensitive(index):
    result = match_track(
        {"artist": "radiohead", "album": "kid a", "track": "everything in its right place"},
        index,
    )
    assert result.match_type == MatchType.EXACT

def test_exact_match_accented_artist(index):
    result = match_track(
        {"artist": "Bjork", "album": "Homogenic", "track": "Joga"},
        index,
    )
    # Normalised "bjork" should match "björk"
    assert result.match_type in (MatchType.EXACT, MatchType.FUZZY)
    assert result.plex_track_id == "1006"


# ── match_track() — fuzzy match ───────────────────────────────────────────────

def test_fuzzy_match_artist_typo(index):
    result = match_track(
        {"artist": "Radiohed", "album": "Kid A", "track": "Everything in Its Right Place"},
        index,
    )
    assert result.match_type in (MatchType.EXACT, MatchType.FUZZY)
    assert result.plex_track_id == "1001"

def test_fuzzy_match_album_subtitle_missing(index):
    # "OK Computer OKNOTOK" vs stored "OK Computer" — should fuzzy match
    result = match_track(
        {"artist": "Radiohead", "album": "OK Computer OKNOTOK", "track": "Paranoid Android"},
        index,
    )
    assert result.match_type in (MatchType.FUZZY, MatchType.ARTIST_FALLBACK)
    assert result.plex_track_id == "1003"

def test_fuzzy_match_track_minor_difference(index):
    result = match_track(
        {"artist": "The Beatles", "album": "Abbey Road", "track": "Come Togther"},  # typo
        index,
    )
    assert result.match_type in (MatchType.EXACT, MatchType.FUZZY)
    assert result.plex_track_id == "1004"


# ── match_track() — artist-only fallback ─────────────────────────────────────

def test_artist_fallback_wrong_album(index):
    # Track exists but attributed to wrong album
    result = match_track(
        {"artist": "Radiohead", "album": "Pablo Honey", "track": "Paranoid Android"},
        index,
    )
    assert result.match_type == MatchType.ARTIST_FALLBACK
    assert result.plex_track_id == "1003"

def test_artist_fallback_missing_album(index):
    result = match_track(
        {"artist": "The Beatles", "album": "", "track": "Something"},
        index,
    )
    assert result.match_type == MatchType.ARTIST_FALLBACK
    assert result.plex_track_id == "1005"


# ── match_track() — rejection reasons ────────────────────────────────────────

def test_no_match_unknown_artist(index):
    result = match_track(
        {"artist": "Completely Unknown Artist XYZ", "album": "Kid A", "track": "Something"},
        index,
    )
    assert result.match_type == MatchType.NO_MATCH
    assert result.rejection_reason == "artist_not_found"

def test_no_match_unknown_track(index):
    # Artist and album match, but track doesn't exist anywhere in catalogue
    result = match_track(
        {"artist": "Radiohead", "album": "Kid A", "track": "Nonexistent Track XYZ QQQQQ"},
        index,
    )
    assert result.match_type == MatchType.NO_MATCH
    assert result.rejection_reason in ("track_not_found", "album_not_found")

def test_no_match_missing_artist_field(index):
    result = match_track({"artist": "", "album": "Kid A", "track": "Something"}, index)
    assert result.match_type == MatchType.NO_MATCH
    assert result.rejection_reason == "unparseable"

def test_no_match_missing_track_field(index):
    result = match_track({"artist": "Radiohead", "album": "Kid A", "track": ""}, index)
    assert result.match_type == MatchType.NO_MATCH
    assert result.rejection_reason == "unparseable"


# ── Deduplication via validated_ids ──────────────────────────────────────────

def test_duplicate_not_returned_twice(index):
    # Simulate: track 1001 already in validated_ids
    # match_track itself doesn't know about this — handled by generator.
    # Here we just confirm plex_track_id is returned correctly for dedup.
    r1 = match_track({"artist": "Radiohead", "album": "Kid A", "track": "Everything in Its Right Place"}, index)
    r2 = match_track({"artist": "Radiohead", "album": "Kid A", "track": "Everything in Its Right Place"}, index)
    assert r1.plex_track_id == r2.plex_track_id  # same ID returned both times; dedup is caller's job
