"""Tests for app/llm.py — LLM response parsing and repair."""
import pytest
from app.llm import parse_llm_response, _filter_valid_items


# ── Happy path ────────────────────────────────────────────────────────────────

def test_parses_clean_json():
    raw = '[{"artist": "Radiohead", "album": "Kid A", "track": "Everything in Its Right Place"}]'
    result = parse_llm_response(raw)
    assert len(result) == 1
    assert result[0]["artist"] == "Radiohead"
    assert result[0]["album"] == "Kid A"
    assert result[0]["track"] == "Everything in Its Right Place"

def test_parses_multiple_tracks():
    raw = '[{"artist": "A", "album": "B", "track": "C"}, {"artist": "D", "album": "E", "track": "F"}]'
    result = parse_llm_response(raw)
    assert len(result) == 2

def test_returns_empty_list_for_empty_input():
    assert parse_llm_response("") == []

def test_returns_empty_list_for_whitespace():
    assert parse_llm_response("   ") == []


# ── Markdown code fences ──────────────────────────────────────────────────────

def test_strips_json_code_fence():
    raw = '```json\n[{"artist": "A", "album": "B", "track": "C"}]\n```'
    result = parse_llm_response(raw)
    assert len(result) == 1

def test_strips_plain_code_fence():
    raw = '```\n[{"artist": "A", "album": "B", "track": "C"}]\n```'
    result = parse_llm_response(raw)
    assert len(result) == 1

def test_strips_preamble_and_postamble():
    raw = 'Here is your playlist:\n[{"artist": "A", "album": "B", "track": "C"}]\nEnjoy!'
    result = parse_llm_response(raw)
    assert len(result) == 1


# ── JSON repair ───────────────────────────────────────────────────────────────

def test_repairs_trailing_comma_in_array():
    raw = '[{"artist": "A", "album": "B", "track": "C"},]'
    result = parse_llm_response(raw)
    assert len(result) == 1

def test_repairs_trailing_comma_in_object():
    raw = '[{"artist": "A", "album": "B", "track": "C",}]'
    result = parse_llm_response(raw)
    assert len(result) == 1

def test_repairs_unclosed_array():
    # LLM cut off mid-response
    raw = '[{"artist": "A", "album": "B", "track": "C"}'
    result = parse_llm_response(raw)
    assert len(result) == 1

def test_repairs_single_quotes():
    raw = "[{'artist': 'A', 'album': 'B', 'track': 'C'}]"
    result = parse_llm_response(raw)
    assert len(result) == 1
    assert result[0]["artist"] == "A"


# ── Per-item validation ───────────────────────────────────────────────────────

def test_discards_item_missing_artist():
    raw = '[{"album": "B", "track": "C"}]'
    result = parse_llm_response(raw)
    assert result == []

def test_discards_item_missing_track():
    raw = '[{"artist": "A", "album": "B"}]'
    result = parse_llm_response(raw)
    assert result == []

def test_keeps_item_missing_album():
    # Album is optional — artist + track is enough for artist-fallback matching
    raw = '[{"artist": "A", "track": "C"}]'
    result = parse_llm_response(raw)
    assert len(result) == 1
    assert result[0]["album"] == ""

def test_discards_non_dict_items():
    raw = '[{"artist": "A", "album": "B", "track": "C"}, "not a dict", 42]'
    result = parse_llm_response(raw)
    assert len(result) == 1

def test_mixed_valid_and_invalid_items():
    raw = '[{"artist": "A", "album": "B", "track": "C"}, {"album": "B", "track": "C"}, {"artist": "D", "album": "E", "track": "F"}]'
    result = parse_llm_response(raw)
    assert len(result) == 2

def test_strips_whitespace_from_fields():
    raw = '[{"artist": "  Radiohead  ", "album": "  Kid A  ", "track": "  Everything  "}]'
    result = parse_llm_response(raw)
    assert result[0]["artist"] == "Radiohead"
    assert result[0]["track"] == "Everything"


# ── Total parse failures ──────────────────────────────────────────────────────

def test_returns_empty_on_totally_unparseable():
    raw = "Sorry, I cannot generate a playlist for that request."
    result = parse_llm_response(raw)
    assert result == []

def test_returns_empty_on_object_not_array():
    # LLM returns a dict instead of a list
    raw = '{"artist": "A", "album": "B", "track": "C"}'
    result = parse_llm_response(raw)
    assert result == []

def test_extracts_array_from_nested_structure():
    # LLM wraps response in {"playlist": [...]} — we recover the inner array
    raw = '{"playlist": [{"artist": "A", "album": "B", "track": "C"}]}'
    result = parse_llm_response(raw)
    assert len(result) == 1
    assert result[0]["artist"] == "A"
