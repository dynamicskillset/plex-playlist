"""Tests for app/llm.py — provider dispatch, request format, response parsing."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.llm import (
    LLMConfig,
    _call_anthropic,
    _call_google,
    _call_openai_compatible,
    call_llm,
    default_context_window,
    parse_llm_response,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _openai_config(**kwargs):
    return LLMConfig(
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        model="gpt-4o-mini",
        context_window=128_000,
        provider="openai",
        **kwargs,
    )


def _anthropic_config(**kwargs):
    return LLMConfig(
        base_url="https://api.anthropic.com/v1",
        api_key="ant-test",
        model="claude-haiku-4-5",
        context_window=200_000,
        provider="anthropic",
        **kwargs,
    )


def _google_config(**kwargs):
    return LLMConfig(
        base_url="",
        api_key="AIza-test",
        model="gemini-2.5-flash",
        context_window=1_000_000,
        provider="google",
        **kwargs,
    )


def _mock_response(json_data: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


TRACK_JSON = '[{"artist": "Radiohead", "album": "OK Computer", "track": "Karma Police"}]'


# ── Provider dispatch ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_llm_dispatches_openai():
    with patch("app.llm._call_openai_compatible", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await call_llm(_openai_config(), "sys", "user")
        mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_llm_dispatches_anthropic():
    with patch("app.llm._call_anthropic", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await call_llm(_anthropic_config(), "sys", "user")
        mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_llm_dispatches_google():
    with patch("app.llm._call_google", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await call_llm(_google_config(), "sys", "user")
        mock.assert_awaited_once()


# ── OpenAI-compatible ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_request_format():
    response = _mock_response({
        "choices": [{"message": {"content": TRACK_JSON}}]
    })
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=response)
        mock_client_cls.return_value = mock_client

        result = await _call_openai_compatible(_openai_config(), "sys", "user")

    call_kwargs = mock_client.post.call_args
    assert "/chat/completions" in call_kwargs[0][0]
    payload = call_kwargs[1]["json"]
    assert payload["model"] == "gpt-4o-mini"
    assert any(m["role"] == "system" for m in payload["messages"])
    assert len(result) == 1
    assert result[0]["artist"] == "Radiohead"


# ── Anthropic ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anthropic_request_format():
    response = _mock_response({
        "content": [{"text": TRACK_JSON}]
    })
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=response)
        mock_client_cls.return_value = mock_client

        result = await _call_anthropic(_anthropic_config(), "sys", "user")

    call_kwargs = mock_client.post.call_args
    assert "/messages" in call_kwargs[0][0]
    headers = call_kwargs[1]["headers"]
    assert "x-api-key" in headers
    assert "anthropic-version" in headers
    payload = call_kwargs[1]["json"]
    # System prompt must be top-level, NOT inside messages
    assert payload["system"] == "sys"
    assert not any(m.get("role") == "system" for m in payload["messages"])
    assert len(result) == 1


@pytest.mark.asyncio
async def test_anthropic_uses_default_base_url_when_empty():
    cfg = LLMConfig(base_url="", api_key="ant-test", model="claude-haiku-4-5",
                    context_window=200_000, provider="anthropic")
    response = _mock_response({"content": [{"text": "[]"}]})
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=response)
        mock_client_cls.return_value = mock_client

        await _call_anthropic(cfg, "sys", "user")

    url = mock_client.post.call_args[0][0]
    assert "anthropic.com" in url


# ── Google ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_google_request_format():
    response = _mock_response({
        "candidates": [{"content": {"parts": [{"text": TRACK_JSON}]}}]
    })
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=response)
        mock_client_cls.return_value = mock_client

        result = await _call_google(_google_config(), "sys", "user")

    call_kwargs = mock_client.post.call_args
    url = call_kwargs[0][0]
    assert "generativelanguage.googleapis.com" in url
    assert "gemini-2.5-flash" in url
    assert "AIza-test" in url
    payload = call_kwargs[1]["json"]
    assert "systemInstruction" in payload
    assert payload["systemInstruction"]["parts"][0]["text"] == "sys"
    assert len(result) == 1


# ── Context window defaults ───────────────────────────────────────────────────

@pytest.mark.parametrize("model,expected", [
    ("gpt-4o-mini", 128_000),
    ("claude-haiku-4-5", 200_000),
    ("gemini-1.5-pro", 1_000_000),
    ("gemini-2.5-flash", 1_000_000),
    ("mistral-large-latest", 128_000),
    ("unknown-model-xyz", 8_192),
])
def test_default_context_window(model, expected):
    assert default_context_window(model) == expected


# ── Response parsing ──────────────────────────────────────────────────────────

def test_parse_clean_json():
    raw = '[{"artist": "A", "album": "B", "track": "C"}]'
    result = parse_llm_response(raw)
    assert result == [{"artist": "A", "album": "B", "track": "C"}]


def test_parse_strips_markdown_fences():
    raw = "```json\n[{\"artist\": \"A\", \"album\": \"B\", \"track\": \"C\"}]\n```"
    result = parse_llm_response(raw)
    assert len(result) == 1


def test_parse_repairs_trailing_comma():
    raw = '[{"artist": "A", "album": "B", "track": "C",}]'
    result = parse_llm_response(raw)
    assert len(result) == 1


def test_parse_discards_items_missing_track():
    raw = '[{"artist": "A", "album": "B"}, {"artist": "X", "album": "Y", "track": "Z"}]'
    result = parse_llm_response(raw)
    assert len(result) == 1
    assert result[0]["track"] == "Z"


def test_parse_empty_response():
    assert parse_llm_response("") == []
    assert parse_llm_response("```\n```") == []
