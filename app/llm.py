"""LLM integration — OpenAI-compatible API via httpx, context building, response parsing."""
import json
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

RETRY_WAIT_SECONDS = 10
MAX_RETRIES = 1  # one retry on 429/5xx per pass

# Known context window defaults (tokens) per model substring
CONTEXT_WINDOW_DEFAULTS = {
    "gpt-4o": 128_000,
    "gpt-4": 128_000,
    "gpt-3.5": 16_385,
    "claude-3-5": 200_000,
    "claude-3": 200_000,
    "claude-sonnet": 200_000,
    "claude-opus": 200_000,
    "claude-haiku": 200_000,
    "llama": 8_192,
    "mistral": 32_768,
    "gemma": 8_192,
}

CHARS_PER_TOKEN = 4  # heuristic for token estimation


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    context_window: int
    temperature: float = 0.9


def default_context_window(model: str) -> int:
    model_lower = model.lower()
    for key, size in CONTEXT_WINDOW_DEFAULTS.items():
        if key in model_lower:
            return size
    return 8_192


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def build_context(
    album_list: list[str],
    system_prompt: str,
    user_prompt: str,
    config: LLMConfig,
    already_selected: list[dict] | None = None,
    batch_count: int = 50,
    is_backfill: bool = False,
    is_full_regeneration: bool = False,
) -> tuple[str, str]:
    """Build system and user message strings, truncating album_list if needed.

    Returns (system_msg, user_msg).
    """
    already_selected = already_selected or []
    selected_block = ""
    if already_selected:
        lines = [f'  {i+1}. "{t["track"]}" by {t["artist"]} ({t["album"]})'
                 for i, t in enumerate(already_selected)]
        selected_block = "The following tracks have already been selected for this playlist:\n" + "\n".join(lines) + "\n\n"

    # Build user message without album list first to gauge fixed overhead
    if is_backfill:
        action = (
            f"{selected_block}"
            f"I need {batch_count} more tracks for the same situation: \"{user_prompt}\"\n\n"
            "Some of your previous suggestions could not be verified in the library. "
            f"Suggest {batch_count} additional tracks, being especially careful to "
            "suggest tracks you are confident exist on the albums listed above. "
            "Do not repeat any track from the already-selected list. Return only the JSON array."
        )
    elif already_selected:
        action = (
            f"{selected_block}"
            f"I need {batch_count} more tracks for the same situation: \"{user_prompt}\"\n\n"
            f"Suggest {batch_count} additional tracks from the library above. Do not "
            "repeat any track from the already-selected list. Vary your selections — "
            "avoid leaning on the same artists already well-represented above. "
            "Return only the JSON array."
        )
    else:
        action = (
            f"Create a playlist of {batch_count} tracks for the following situation:\n"
            f"\"{user_prompt}\""
        )

    # Truncate album list to fit context window
    overhead_tokens = estimate_tokens(system_prompt + action) + 500  # 500 token buffer
    budget = int(config.context_window * 0.8) - overhead_tokens
    truncated_albums = _truncate_album_list(album_list, budget, user_prompt)

    library_block = "Here is my music library:\n\n" + "\n".join(truncated_albums)
    user_msg = f"{library_block}\n\n{action}"

    sys_msg = system_prompt
    if is_full_regeneration:
        sys_msg += (
            "\n\nThis is a regeneration of an existing playlist. Aim for a fresh selection "
            "that differs meaningfully from what might have been generated previously. "
            "Surprise the listener with less obvious choices where they fit the situation."
        )

    return sys_msg, user_msg


def _truncate_album_list(albums: list[str], token_budget: int, prompt: str) -> list[str]:
    """Truncate album list to fit within token budget.

    Priority: albums whose text overlaps with prompt keywords.
    """
    if estimate_tokens("\n".join(albums)) <= token_budget:
        return albums

    prompt_words = set(re.findall(r'\w+', prompt.lower()))

    def relevance(line: str) -> int:
        words = set(re.findall(r'\w+', line.lower()))
        return len(words & prompt_words)

    scored = sorted(enumerate(albums), key=lambda x: relevance(x[1]), reverse=True)
    result = []
    used_tokens = 0
    for _, line in scored:
        t = estimate_tokens(line) + 1
        if used_tokens + t > token_budget:
            break
        result.append(line)
        used_tokens += t

    # Restore original order
    selected_set = set(result)
    return [a for a in albums if a in selected_set]


SYSTEM_PROMPT = (
    "You are a music playlist curator. You will receive a list of artists "
    "and albums available in the user's music library. You must ONLY suggest tracks "
    "from these artists and albums. Do not suggest any artist or album not in the "
    "provided list.\n\n"
    "Return your suggestions as a JSON array and nothing else — no commentary, "
    "no markdown formatting, no preamble. The response must be valid JSON.\n\n"
    'Format: [{"artist": "...", "album": "...", "track": "..."}, ...]\n\n'
    "If you are unsure whether a track exists on a given album, prefer tracks you "
    "are confident about. Prefer variety across artists. Do not repeat tracks."
)


async def call_llm(
    config: LLMConfig,
    system_msg: str,
    user_msg: str,
) -> list[dict]:
    """Call the LLM and return parsed track suggestions.

    Retries once on 429/5xx. Raises on unrecoverable failure.
    Returns list of {"artist", "album", "track"} dicts (may be empty on parse failure).
    """
    import asyncio

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{config.base_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < MAX_RETRIES:
                        logger.warning("LLM returned %d, retrying in %ds", resp.status_code, RETRY_WAIT_SECONDS)
                        await asyncio.sleep(RETRY_WAIT_SECONDS)
                        continue
                    resp.raise_for_status()
                resp.raise_for_status()
                data = resp.json()
                raw = data["choices"][0]["message"]["content"]
                return parse_llm_response(raw)
        except httpx.HTTPStatusError:
            raise
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            raise

    return []


def parse_llm_response(raw: str) -> list[dict]:
    """Parse LLM response into list of track dicts.

    Handles: markdown fences, preamble, trailing commas, single quotes,
    unclosed brackets, partial extraction.
    """
    if not raw:
        return []

    # Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()
    text = re.sub(r"```\s*$", "", text).strip()

    # Attempt 1: direct parse
    result = _try_parse(text)
    if result is not None:
        return _filter_valid_items(result)

    # Attempt 2: extract JSON array substring
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        result = _try_parse(match.group(0))
        if result is not None:
            return _filter_valid_items(result)

    # Attempt 3: basic repair — trailing commas, single → double quotes
    repaired = _repair_json(text)
    result = _try_parse(repaired)
    if result is not None:
        return _filter_valid_items(result)

    # Attempt 4: repair + extract array
    match = re.search(r"\[.*\]", repaired, re.DOTALL)
    if match:
        result = _try_parse(match.group(0))
        if result is not None:
            return _filter_valid_items(result)

    logger.error("Failed to parse LLM response. Raw (first 500 chars): %s", raw[:500])
    return []


def _try_parse(text: str) -> list | None:
    try:
        val = json.loads(text)
        if isinstance(val, list):
            return val
    except json.JSONDecodeError:
        pass
    return None


def _repair_json(text: str) -> str:
    # Remove trailing commas before ] or }
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Single quotes to double (naive but catches common cases)
    text = text.replace("'", '"')
    # Close unclosed arrays
    if text.count("[") > text.count("]"):
        text = text.rstrip().rstrip(",") + "]"
    return text


def _filter_valid_items(items: list) -> list[dict]:
    """Discard items missing required fields."""
    valid = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("artist") and item.get("track"):
            valid.append({
                "artist": str(item["artist"]).strip(),
                "album": str(item.get("album", "")).strip(),
                "track": str(item["track"]).strip(),
            })
    return valid


async def validate_llm_connection(config: LLMConfig) -> bool:
    """Make a minimal test call to verify the LLM config is working."""
    try:
        result = await call_llm(
            config,
            system_msg="Reply with a valid JSON array containing one item.",
            user_msg='[{"artist": "Test", "album": "Test", "track": "Test"}]',
        )
        return True
    except Exception as e:
        logger.warning("LLM validation failed: %s", e)
        return False
