"""Playlist generation pipeline — batching, backfill, validation."""
import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable

from .llm import LLMConfig, SYSTEM_PROMPT, build_context, call_llm
from .matching import LibraryIndex, MatchType, match_track

logger = logging.getLogger(__name__)

BATCH_SIZE = 60
MAX_BACKFILL_RETRIES = 3
MIN_FLOOR = 20


@dataclass
class GenerationStats:
    llm_passes: int = 0
    tracks_suggested: int = 0
    tracks_validated: int = 0
    match_exact: int = 0
    match_fuzzy: int = 0
    match_artist_fallback: int = 0
    rejected_artist_not_found: int = 0
    rejected_album_not_found: int = 0
    rejected_track_not_found: int = 0
    rejected_unparseable: int = 0
    used_sonic_analysis: bool = False


@dataclass
class GenerationResult:
    success: bool
    validated_tracks: list[dict] = field(default_factory=list)  # [{"artist", "album", "track", "plex_track_id"}]
    stats: GenerationStats = field(default_factory=GenerationStats)
    error: str = ""


ProgressCallback = Callable[[str], None]


async def generate_playlist(
    prompt: str,
    target_count: int,
    index: LibraryIndex,
    llm_config: LLMConfig,
    album_list: list[str],
    sonic_data: dict | None = None,
    already_validated: list[dict] | None = None,
    is_full_regeneration: bool = False,
    progress: ProgressCallback | None = None,
) -> GenerationResult:
    """Run the full grounded generation pipeline.

    already_validated: tracks already in playlist (for append-only refresh)
    Returns GenerationResult.
    """
    def emit(msg: str):
        if progress:
            progress(msg)
        logger.info(msg)

    stats = GenerationStats(used_sonic_analysis=sonic_data is not None)
    validated: list[dict] = list(already_validated or [])
    validated_ids: set[str] = {t["plex_track_id"] for t in validated}

    remaining = target_count - len(validated)
    if remaining <= 0:
        return GenerationResult(success=True, validated_tracks=validated, stats=stats)

    emit("Querying library...")

    # Build batches
    batches = []
    while remaining > 0:
        batches.append(min(BATCH_SIZE, remaining))
        remaining -= BATCH_SIZE

    total_batches = len(batches)
    emit(f"Starting generation — {total_batches} batch(es) of up to {BATCH_SIZE} tracks")

    for batch_num, batch_count in enumerate(batches, 1):
        emit(f"Generating suggestions (batch {batch_num}/{total_batches})...")
        batch_validated = await _run_batch(
            prompt=prompt,
            batch_count=batch_count,
            batch_num=batch_num,
            total_batches=total_batches,
            index=index,
            llm_config=llm_config,
            album_list=album_list,
            sonic_data=sonic_data,
            already_validated=validated,
            stats=stats,
            validated_ids=validated_ids,
            is_full_regeneration=is_full_regeneration,
            progress=emit,
        )
        for t in batch_validated:
            if t["plex_track_id"] not in validated_ids:
                validated.append(t)
                validated_ids.add(t["plex_track_id"])

    stats.tracks_validated = len(validated) - len(already_validated or [])

    if len(validated) < MIN_FLOOR:
        error = (
            f"Only {len(validated)} valid tracks found — below minimum of {MIN_FLOOR}. "
            "Try broadening your prompt or adding more music to your library."
        )
        emit(f"Failed — {error}")
        return GenerationResult(success=False, validated_tracks=validated, stats=stats, error=error)

    if len(validated) < target_count:
        emit(f"Completed with {len(validated)}/{target_count} tracks (minimum met)")
    else:
        emit(f"Done — {len(validated)} tracks ready")

    return GenerationResult(success=True, validated_tracks=validated, stats=stats)


async def _run_batch(
    prompt: str,
    batch_count: int,
    batch_num: int,
    total_batches: int,
    index: LibraryIndex,
    llm_config: LLMConfig,
    album_list: list[str],
    sonic_data: dict | None,
    already_validated: list[dict],
    stats: GenerationStats,
    validated_ids: set[str],
    is_full_regeneration: bool,
    progress: ProgressCallback,
) -> list[dict]:
    """Run one batch with up to MAX_BACKFILL_RETRIES backfill passes."""
    batch_validated: list[dict] = []
    needed = batch_count

    for attempt in range(MAX_BACKFILL_RETRIES + 1):
        is_backfill = attempt > 0
        if is_backfill:
            progress(f"Running backfill pass {attempt} (batch {batch_num}/{total_batches}, need {needed} more)...")

        sys_msg, user_msg = build_context(
            album_list=album_list,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            config=llm_config,
            already_selected=already_validated + batch_validated,
            batch_count=needed,
            is_backfill=is_backfill,
            is_full_regeneration=is_full_regeneration and attempt == 0 and batch_num == 1,
        )

        try:
            suggestions = await call_llm(llm_config, sys_msg, user_msg)
        except Exception as e:
            logger.error("LLM call failed on batch %d attempt %d: %s", batch_num, attempt, e)
            stats.llm_passes += 1
            if attempt >= MAX_BACKFILL_RETRIES:
                break
            continue

        stats.llm_passes += 1
        stats.tracks_suggested += len(suggestions)

        newly_validated = _validate_suggestions(suggestions, index, validated_ids, stats)
        for t in newly_validated:
            batch_validated.append(t)
            validated_ids.add(t["plex_track_id"])

        progress(f"Validating tracks ({len(batch_validated)}/{batch_count})...")
        needed = batch_count - len(batch_validated)

        if needed <= 0:
            break

    return batch_validated


def _validate_suggestions(
    suggestions: list[dict],
    index: LibraryIndex,
    already_validated_ids: set[str],
    stats: GenerationStats,
) -> list[dict]:
    """Validate suggestions against library index. Returns newly validated tracks."""
    validated = []
    for suggestion in suggestions:
        result = match_track(suggestion, index)
        if result.match_type == MatchType.NO_MATCH:
            reason = result.rejection_reason or "artist_not_found"
            if reason == "artist_not_found":
                stats.rejected_artist_not_found += 1
            elif reason == "album_not_found":
                stats.rejected_album_not_found += 1
            elif reason == "track_not_found":
                stats.rejected_track_not_found += 1
            else:
                stats.rejected_unparseable += 1
            continue

        if result.plex_track_id in already_validated_ids:
            continue

        if result.match_type == MatchType.EXACT:
            stats.match_exact += 1
        elif result.match_type == MatchType.FUZZY:
            stats.match_fuzzy += 1
        elif result.match_type == MatchType.ARTIST_FALLBACK:
            stats.match_artist_fallback += 1

        validated.append({
            "artist": suggestion["artist"],
            "album": suggestion.get("album", ""),
            "track": suggestion["track"],
            "plex_track_id": result.plex_track_id,
        })

    return validated
