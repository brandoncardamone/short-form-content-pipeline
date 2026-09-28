"""
Original narrated monologues - confession, diary, voicemail, rant.

Unlike textchain (a conversation rendered as chat bubbles) and reddit_story
(someone else's post), this is one voice talking straight at the viewer, and it
renders through the caption format. No scraping, no source licence questions,
and the tone is fully controllable through the prompt.

Form and theme are drawn at random per script for the same reason textchain
does it: left alone the model converges on one shape and every video starts to
announce itself as the same video.
"""

import json
import logging
import random
from typing import Optional

from src.schema import Script, Beat
from src.generate.llm import load_client, extract_json
from src.captions import build_caption
from src.db import premise_exists
from src.render.caption_cards import strip_emphasis

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

FORMS = [
    ("confession", "a confession the speaker has never told anyone, admitted to camera"),
    ("diary", "a diary entry read aloud, present tense, as it is happening"),
    ("voicemail", "a voicemail left for someone who will not pick up"),
    ("rant", "someone venting about something that just happened to them"),
    ("warning", "someone warning strangers about something they learned the hard way"),
    ("letter", "a letter to someone the speaker can no longer talk to"),
    ("apology", "an apology that keeps revealing more than it means to"),
]

THEMES = [
    "a favour that turned out to have strings attached",
    "finding something in a house you just moved into",
    "a coworker who is not who they said they were",
    "a family member's secret that explains everything",
    "a neighbour whose routine does not add up",
    "a friendship that ended over one sentence",
    "money that appeared and should not have",
    "a job interview that went somewhere strange",
    "an old photo that should not exist",
    "a message received years too late",
    "realising you were the problem",
    "a stranger who knew your name",
]

EXCLUDED = [
    "graphic violence",
    "sexual content",
    "self-harm or suicide",
    "content involving minors in any charged context",
    "hate speech or slurs",
]

PROMPT = """You write narration for a short vertical video. One voice, talking straight at the viewer,
shown as big full-screen captions over gameplay footage.

Form for this script: {form_desc}
Theme for this script: {theme}

Rules:
- Open mid-thought, on the most alarming or specific detail. Never set the scene first, never
  open with "so" or "I need to tell you something" - those are skipped instantly.
- {beats} beats, each ONE short sentence of roughly 4-12 words. Each beat is a full-screen
  caption shown alone, so a long beat is a wall of text.
- Plain spoken English. Contractions. No literary phrasing, no "little did I know".
- Every beat must add something. Cut anything a viewer would already assume.
- Concrete and specific: names, objects, times, places. Specificity is what makes it feel real.
- Around 60% through, it should look like it resolves. Then the real thing lands.
- In each beat you may wrap ONE word in *asterisks* to mark it for visual emphasis. Pick the word
  carrying the surprise. Not every beat needs one.
- The last beat is a direct question to the viewer that invites a comment.
- Strictly excluded: {excluded}

Output valid JSON only, no markdown fences:
{{
  "title": "short internal title",
  "hook": "the first beat's text, without asterisks",
  "premise": "one-line summary for deduplication",
  "tags": ["tag1", "tag2", "tag3", "tag4"],
  "beats": ["first beat", "second beat", ...]
}}"""


def _target_beats(cfg) -> int:
    """Caption beats are short; roughly 2.2s each."""
    mid = (cfg.video.target_duration_min + cfg.video.target_duration_max) / 2
    return max(8, round(mid / 2.2))


def generate_monologue_script(cfg, db_conn) -> Script:
    client = load_client(cfg)
    n_beats = _target_beats(cfg)
    temperature = 0.95

    for attempt in range(1, MAX_RETRIES + 1):
        form, form_desc = random.choice(FORMS)
        theme = random.choice(THEMES)
        logger.info("Monologue: form=%r theme=%r beats=%d", form, theme, n_beats)

        prompt = PROMPT.format(
            form_desc=form_desc, theme=theme, beats=n_beats,
            excluded=", ".join(EXCLUDED),
        )
        raw = client.complete(prompt, temperature=temperature)

        try:
            script = _parse(extract_json(raw), cfg, form)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("Monologue parse failed (attempt %d): %s", attempt, e)
            if attempt == MAX_RETRIES:
                raise
            temperature = min(temperature + 0.1, 1.4)
            continue

        if db_conn is not None and premise_exists(db_conn, script.premise):
            logger.warning("Duplicate premise (attempt %d): %s", attempt, script.premise)
            if attempt == MAX_RETRIES:
                raise ValueError("Could not generate a non-duplicate monologue")
            temperature = min(temperature + 0.15, 1.5)
            continue

        logger.info("Monologue accepted: %r (%s, %d beats)", script.title, form, len(script.beats))
        return script

    raise RuntimeError("Monologue generation loop exited without returning")


def _parse(data: dict, cfg, form: str) -> Script:
    voice = cfg.tts.voices.a
    raw_beats = [b for b in (data.get("beats") or []) if str(b).strip()]
    if len(raw_beats) < 4:
        raise ValueError(f"Only {len(raw_beats)} beats")

    # Beat.text keeps the *markers*; TTS strips them so they are never spoken.
    beats = [Beat(text=str(b).strip(), speaker="a", voice=voice) for b in raw_beats]

    hook = strip_emphasis(str(data.get("hook") or raw_beats[0])).strip()
    tags = data.get("tags") or ["storytime", "confession", "fyp"]
    caption = build_caption("monologue", hook, tags)

    return Script(
        beats=beats,
        title=str(data["title"])[:120],
        hook=hook,
        tags=tags,
        caption=caption,
        premise=str(data["premise"]),
    )
