"""
Wikipedia "On This Day" source - the first non-Reddit source in the pipeline.

Uses the Wikimedia feed API, which is free, needs no auth and no app approval:
    https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/MM/DD
That matters because every other route into Reddit-style content has run into
either a paid tier or an approval process (see HANDOFF.md on Reddit's API and
Project Arctic Shift).

The API returns real, dated historical events with a short description. The LLM
is used only to REWRITE a chosen event into short-form narration, never to
supply facts - the prompt gets the source text and is told it may not add
anything that is not in it. Wikipedia text is CC BY-SA, so rewriting into
original narration rather than reading it verbatim is also the right call
licence-wise.

Renders through the caption format (no card), so it does not have to pretend to
be a Reddit post.
"""

import json
import logging
import random
import time
from datetime import date, timedelta
from typing import Optional

import requests

from src.schema import Script, Beat
from src.generate.llm import load_client, extract_json
from src.captions import build_caption
from src.db import premise_exists
from src.render.caption_cards import strip_emphasis, target_beats as _target_beats

logger = logging.getLogger(__name__)

FEED_BASE = "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events"
USER_AGENT = "short-form-content-pipeline/1.0 (personal project)"
MAX_RETRIES = 3
FETCH_DAYS = 6          # distinct calendar days sampled per attempt
MIN_EVENT_CHARS = 120   # a one-line stub cannot carry a 40s video

# Same bounds as the other generators. Checked against the SOURCE text before
# an event is ever sent to the LLM, because "on this day" is disproportionately
# wars, massacres and executions.
EXCLUDED_TERMS = (
    "massacre", "genocide", "execution", "executed", "hanged", "beheaded",
    "rape", "raped", "murder", "murdered", "lynch", "suicide", "atrocit",
    "concentration camp", "holocaust", "terrorist", "bombing", "shooting",
    "killed", "death toll", "casualties", "slaughter", "assassinat",
)

PROMPT = """You rewrite a real historical event into narration for a short vertical video.
The video shows big captions over gameplay footage; the narration is read aloud by one voice.

SOURCE EVENT (year {year}):
{text}

Rules:
- Use ONLY facts present in the source above. Do not add names, numbers, causes or consequences
  that are not there. If the source is thin, stay thin - do not invent to fill time.
- Open on the single most surprising detail. Never open with "on this day" or "in {year}" -
  that is how every history video opens and it is instantly skippable.
- Conversational and plain. Short sentences. No documentary voice, no "little did they know".
- {beats} beats, each ONE short sentence of roughly 4-12 words. They are shown as full-screen
  captions one at a time, so a long beat means a wall of text on screen.
- In each beat you may wrap ONE word in *asterisks* to mark it for visual emphasis. Choose the
  word carrying the surprise. Not every beat needs one.
- The final beat is a question to the viewer that invites a comment.

Output valid JSON only, no markdown fences:
{{
  "title": "short internal title",
  "hook": "the first beat's text, without asterisks",
  "premise": "one-line summary for deduplication",
  "tags": ["tag1", "tag2", "tag3", "tag4"],
  "beats": ["first beat", "second beat", ...]
}}"""


def _fetch_events(cfg) -> list[dict]:
    """Pool events from several random calendar days."""
    events: list[dict] = []
    for _ in range(FETCH_DAYS):
        d = date(2024, 1, 1) + timedelta(days=random.randrange(366))
        url = f"{FEED_BASE}/{d.month:02d}/{d.day:02d}"
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Wikipedia feed %s failed: %s", url, e)
            time.sleep(1)
            continue
        for ev in resp.json().get("events", []):
            if ev.get("year") and len(ev.get("text", "")) >= MIN_EVENT_CHARS:
                events.append(ev)
    random.shuffle(events)
    return events


def _is_flagged(text: str) -> bool:
    low = text.lower()
    return any(term in low for term in EXCLUDED_TERMS)




def generate_wiki_script(cfg, db_conn) -> Script:
    """Pick a real event and rewrite it into caption narration."""
    client = load_client(cfg)
    n_beats = _target_beats(cfg)

    for attempt in range(1, MAX_RETRIES + 1):
        events = _fetch_events(cfg)
        if not events:
            raise RuntimeError("Wikipedia feed returned no usable events")

        for ev in events:
            source_text = ev["text"].strip()
            if _is_flagged(source_text):
                continue
            if db_conn is not None and premise_exists(db_conn, f"wiki:{ev['year']}:{source_text[:80]}"):
                continue

            prompt = PROMPT.format(year=ev["year"], text=source_text, beats=n_beats)
            raw = client.complete(prompt, temperature=0.85)
            try:
                data = extract_json(raw)
                script = _parse(data, cfg, ev)
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.warning("Wikipedia script parse failed: %s", e)
                continue

            if db_conn is not None and premise_exists(db_conn, script.premise):
                logger.info("Duplicate premise, trying another event")
                continue

            logger.info("Wikipedia script accepted: %r (year %s, %d beats)",
                        script.title, ev["year"], len(script.beats))
            return script

        logger.warning("No usable event in this pool (attempt %d/%d)", attempt, MAX_RETRIES)

    raise RuntimeError(f"Could not build a Wikipedia script after {MAX_RETRIES} attempts")


def _parse(data: dict, cfg, ev: dict) -> Script:
    voice = cfg.tts.voices.a
    raw_beats = [b for b in (data.get("beats") or []) if str(b).strip()]
    if len(raw_beats) < 4:
        raise ValueError(f"Only {len(raw_beats)} beats")

    # Beat.text keeps the *markers* for the renderer; TTS strips them. Keeping
    # one field rather than two means the timing contract is unchanged.
    beats = [Beat(text=str(b).strip(), speaker="a", voice=voice) for b in raw_beats]

    hook = strip_emphasis(str(data.get("hook") or raw_beats[0])).strip()
    tags = data.get("tags") or ["history", "facts", "todayilearned", "fyp"]
    caption = build_caption("wiki_facts", hook, tags)

    return Script(
        beats=beats,
        title=str(data["title"])[:120],
        hook=hook,
        tags=tags,
        caption=caption,
        premise=str(data["premise"]),
    )
