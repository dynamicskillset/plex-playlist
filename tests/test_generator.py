"""Tests for app/generator.py — pipeline, batching, deduplication."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.generator import generate_playlist, GenerationStats
from app.matching import LibraryIndex
from app.llm import LLMConfig


@pytest.fixture
def index():
    idx = LibraryIndex()
    for i in range(100):
        idx.add_track(f"Artist {i}", f"Album {i}", f"Track {i}", str(1000 + i))
    return idx


@pytest.fixture
def llm_config():
    return LLMConfig(
        base_url="http://fake",
        api_key="fake",
        model="gpt-4o",
        context_window=128_000,
        temperature=0.9,
    )


def _make_suggestions(start: int, count: int) -> list[dict]:
    return [
        {"artist": f"Artist {start + i}", "album": f"Album {start + i}", "track": f"Track {start + i}"}
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_validated_tracks_actually_populated(index, llm_config):
    """Regression: validated tracks must be non-empty after successful generation."""
    album_list = index.artist_album_list()

    with patch("app.generator.call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_suggestions(0, 50)

        result = await generate_playlist(
            prompt="test prompt",
            target_count=50,
            index=index,
            llm_config=llm_config,
            album_list=album_list,
        )

    assert result.success
    assert len(result.validated_tracks) == 50


@pytest.mark.asyncio
async def test_no_duplicate_tracks_in_result(index, llm_config):
    """Each track should appear at most once even if LLM suggests it twice."""
    album_list = index.artist_album_list()
    duplicates = _make_suggestions(0, 30) + _make_suggestions(0, 30)  # 60 suggestions, 30 unique

    with patch("app.generator.call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = duplicates

        result = await generate_playlist(
            prompt="test prompt",
            target_count=30,
            index=index,
            llm_config=llm_config,
            album_list=album_list,
        )

    track_ids = [t["plex_track_id"] for t in result.validated_tracks]
    assert len(track_ids) == len(set(track_ids))


@pytest.mark.asyncio
async def test_backfill_triggered_when_short(index, llm_config):
    """If first pass returns fewer than target, backfill passes are made."""
    album_list = index.artist_album_list()

    call_count = 0
    async def mock_llm(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_suggestions(0, 10)   # short — triggers backfill
        return _make_suggestions(10, 40)       # backfill fills the rest

    with patch("app.generator.call_llm", new=mock_llm):
        result = await generate_playlist(
            prompt="test prompt",
            target_count=50,
            index=index,
            llm_config=llm_config,
            album_list=album_list,
        )

    assert call_count >= 2
    assert len(result.validated_tracks) >= 10


@pytest.mark.asyncio
async def test_fails_below_minimum_floor(index, llm_config):
    """Generation fails if fewer than 20 tracks validate after all retries."""
    album_list = index.artist_album_list()

    with patch("app.generator.call_llm", new_callable=AsyncMock) as mock_llm:
        # Return tracks that won't match anything in the index
        mock_llm.return_value = [
            {"artist": "Nonexistent XYZ", "album": "Fake Album", "track": "Fake Track"}
        ] * 5

        result = await generate_playlist(
            prompt="test prompt",
            target_count=50,
            index=index,
            llm_config=llm_config,
            album_list=album_list,
        )

    assert not result.success
    assert "below minimum" in result.error


@pytest.mark.asyncio
async def test_stats_track_match_types(index, llm_config):
    """Stats should reflect validated track counts."""
    album_list = index.artist_album_list()

    with patch("app.generator.call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_suggestions(0, 30)

        result = await generate_playlist(
            prompt="test prompt",
            target_count=30,
            index=index,
            llm_config=llm_config,
            album_list=album_list,
        )

    assert result.stats.tracks_validated == 30
    assert result.stats.match_exact == 30
