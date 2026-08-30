"""
Text-chain script generator. Calls the LLM with a structured prompt and returns
a validated Script. Checks premise_hash against the DB before accepting.

Target: 24-36 messages, realistic texting register, bounded themes.
"""

import hashlib
import json
import logging
from typing import Optional

from src.schema import Script, Beat
from src.generate.llm import load_client, extract_json
from src.db import premise_exists, premise_hash

logger = logging.getLogger(__name__)

THEMES = [
    "relationship drama",
    "family secrets",
    "workplace conflict",
    "mild suspense / unexpected revelation",
]

EXCLUDED = [
    "graphic violence",
    "sexual content",
    "self-harm or suicide",
    "content involving minors in any charged context",
    "hate speech or slurs",
]

SYSTEM_PROMPT = """You write short-form social media scripts formatted as iMessage chat conversations.

Rules for message content:
- Realistic texting register: mostly lowercase, missing apostrophes, fragments, typos, abbreviations
- Alternating but uneven: speakers send 1-3 messages in a row, not strict back-and-forth
- Most messages under 12 words. A few longer ones for emotional beats
- Themes: {themes}
- Strictly excluded: {excluded}
- AI-generated disclosure must appear in the caption

Structure:
- Messages 1-2: the hook — the most arresting line of the script
- ~60% through: a false resolution (the conflict seems settled)
- Final messages: the real twist

Output valid JSON only, no markdown fences. Schema:
{{
  "title": "short internal title",
  "hook": "one-sentence hook (same as first message text)",
  "premise": "one-line summary for deduplication",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "caption": "instagram/tiktok caption ending with AI-disclosure line",
  "beats": [
    {{"speaker": "a", "text": "message text"}},
    ...
  ]
}}

Speaker A is the protagonist (sends first). Speaker B is the other party.
Produce exactly {n_messages} messages."""

MAX_RETRIES = 3


def generate_script(cfg, db_conn, n_messages: Optional[int] = None) -> Script:
    """Generate a Script and verify it isn't a duplicate. Retries up to MAX_RETRIES times."""
    client = load_client(cfg)
    n = n_messages or 28   # default 28 messages ≈ 70s at ~2.5s average

    prompt = SYSTEM_PROMPT.format(
        themes=", ".join(THEMES),
        excluded=", ".join(EXCLUDED),
        n_messages=n,
    )

    temperature = 0.9
    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("Generating script (attempt %d, temp=%.2f, n=%d)", attempt, temperature, n)
        raw = client.complete(prompt, temperature=temperature)

        try:
            data = extract_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("JSON parse failed (attempt %d): %s", attempt, e)
            if attempt == MAX_RETRIES:
                raise
            temperature = min(temperature + 0.1, 1.4)
            continue

        try:
            script = _parse_script(data, cfg)
        except (KeyError, ValueError) as e:
            logger.warning("Schema validation failed (attempt %d): %s", attempt, e)
            if attempt == MAX_RETRIES:
                raise
            temperature = min(temperature + 0.1, 1.4)
            continue

        if db_conn is not None and premise_exists(db_conn, script.premise):
            logger.warning("Duplicate premise detected (attempt %d): %s", attempt, script.premise)
            if attempt == MAX_RETRIES:
                raise ValueError(f"Could not generate a non-duplicate script after {MAX_RETRIES} attempts")
            temperature = min(temperature + 0.15, 1.5)
            continue

        logger.info("Script accepted: %r (%d beats)", script.title, len(script.beats))
        return script

    raise RuntimeError("Generation loop exited without returning")  # unreachable


def _parse_script(data: dict, cfg) -> Script:
    voices = cfg.tts.voices

    beats = []
    for i, b in enumerate(data["beats"]):
        sp = b["speaker"].lower().strip()
        if sp not in ("a", "b"):
            raise ValueError(f"Beat {i} has invalid speaker {sp!r}")
        beats.append(Beat(
            text=b["text"],
            speaker=sp,
            voice=voices.a if sp == "a" else voices.b,
        ))

    if not beats:
        raise ValueError("No beats in generated script")

    caption = data.get("caption", "")
    if not any(kw in caption.lower() for kw in ("ai", "generated", "artificial")):
        caption += "\n\n⚠️ AI-generated content."

    return Script(
        beats=beats,
        title=data["title"],
        hook=data["hook"],
        tags=data.get("tags", []),
        caption=caption,
        premise=data["premise"],
    )
