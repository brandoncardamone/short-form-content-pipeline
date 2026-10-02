"""
Two-character explainer - the format behind account two.

An expert explains something to someone asking the questions the viewer is
already thinking. The dialogue is the point: call-and-response keeps attention
in a way monologue narration does not, because every answer arrives as a reply
to a question the viewer just had.

Deliberately original characters. The obvious version of this format borrows
recognisable cartoon characters, and that is both a takedown risk and
disqualifying for monetisation - you cannot monetise content built on IP you do
not own, which defeats the entire point of running the account. The retention
mechanic lives in the structure, not in who is speaking.

Renders through the caption format with speaker names (see caption_cards.py),
so no character art is required.
"""

import json
import logging
import random
from typing import Optional

from src.schema import Script, Beat
from src.generate.llm import load_client, extract_json
from src.captions import build_caption
from src.db import premise_exists
from src.render.caption_cards import strip_emphasis, target_beats as _target_beats

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# Broad on purpose. A channel that only does space, or only does biology, caps
# its own audience; the through-line is the duo, not the subject.
TOPIC_AREAS = [
    "human biology that sounds made up but isn't",
    "physics you can see in everyday objects",
    "psychology of why people do obviously irrational things",
    "how a piece of everyday infrastructure actually works",
    "animal abilities that outclass human technology",
    "economics hidden inside ordinary prices",
    "engineering failures and what they taught us",
    "chemistry happening in your kitchen right now",
    "how your senses lie to you",
    "scale comparisons that break your intuition",
    "medical history that changed how everyone lives",
    "materials science behind something in your pocket",
    "weather and climate mechanisms people get wrong",
    "how money and banking actually move",
    "the maths behind something that looks like luck",
]

EXCLUDED = [
    "graphic violence",
    "sexual content",
    "self-harm or suicide",
    "medical advice presented as actionable",
    "political or religious controversy",
]

PROMPT = """You write short vertical videos where two characters explain something real.

{expert} is the one who knows things: dry, confident, slightly impatient, enjoys a good fact.
{student} is the one asking: curious, blunt, says what the viewer is thinking, pushes back when
something sounds like nonsense. {student} is NOT stupid - the questions are the obvious ones a
smart person would actually ask.

Topic area for this script: {topic_area}
Pick ONE specific, concrete thing inside that area and explain it properly.

Rules:
- Everything stated as fact must be TRUE and uncontroversial. If you are not confident it is
  correct, pick something else. Never invent numbers. Round rather than fabricate precision.
- Open on the single most surprising true claim, said by {expert}. No greeting, no "did you know",
  no setting up what the video is about. The first line IS the hook.
- {student} must push back or ask at least three times. The questions carry the video - each one
  should be what the viewer is thinking at that exact moment.
- {beats} beats total, alternating speakers naturally (not strictly every line - {expert} can run
  two short lines together when explaining).
- Each beat is ONE short sentence, roughly 4-12 words. Beats are full-screen captions shown one at
  a time, so a long beat is a wall of text.
- Plain spoken English. No lecture voice, no "furthermore", no "in conclusion".
- Land a real explanation. The viewer should finish actually understanding the thing, not just
  having heard that it is surprising.
- In each beat you may wrap ONE word in *asterisks* to mark it for visual emphasis.
- Strictly excluded: {excluded}
- The LAST beat is {student} asking the viewer something that invites a reply in the comments.

Output valid JSON only, no markdown fences:
{{
  "title": "short internal title",
  "hook": "the first beat's text, without asterisks",
  "premise": "one-line summary for deduplication",
  "tags": ["tag1", "tag2", "tag3", "tag4"],
  "beats": [
    {{"speaker": "a", "text": "..."}},
    {{"speaker": "b", "text": "..."}}
  ]
}}

Speaker "a" is {expert}. Speaker "b" is {student}."""


def speaker_map(cfg) -> dict:
    """Passed to CaptionRenderer so each beat shows who is talking."""
    e = cfg.explainer
    return {
        "a": {"name": e.expert_name, "color": e.expert_color},
        "b": {"name": e.student_name, "color": e.student_color},
    }


def generate_explainer_script(cfg, db_conn) -> Script:
    client = load_client(cfg)
    n_beats = _target_beats(cfg)
    e = cfg.explainer
    temperature = 0.9

    for attempt in range(1, MAX_RETRIES + 1):
        topic_area = random.choice(TOPIC_AREAS)
        logger.info("Explainer: topic_area=%r beats=%d", topic_area, n_beats)

        prompt = PROMPT.format(
            expert=e.expert_name, student=e.student_name,
            topic_area=topic_area, beats=n_beats,
            excluded=", ".join(EXCLUDED),
        )
        raw = client.complete(prompt, temperature=temperature)

        try:
            script = _parse(extract_json(raw), cfg)
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.warning("Explainer parse failed (attempt %d): %s", attempt, exc)
            if attempt == MAX_RETRIES:
                raise
            temperature = min(temperature + 0.1, 1.3)
            continue

        if db_conn is not None and premise_exists(db_conn, script.premise):
            logger.warning("Duplicate premise (attempt %d): %s", attempt, script.premise)
            if attempt == MAX_RETRIES:
                raise ValueError("Could not generate a non-duplicate explainer")
            temperature = min(temperature + 0.15, 1.4)
            continue

        logger.info("Explainer accepted: %r (%d beats)", script.title, len(script.beats))
        return script

    raise RuntimeError("Explainer generation loop exited without returning")


def _parse(data: dict, cfg) -> Script:
    voices = cfg.tts.voices
    raw = data.get("beats") or []
    beats = []
    for i, b in enumerate(raw):
        sp = str(b.get("speaker", "")).lower().strip()
        text = str(b.get("text", "")).strip()
        if sp not in ("a", "b"):
            raise ValueError(f"Beat {i} has speaker {sp!r}; expected 'a' or 'b'")
        if not text:
            continue
        beats.append(Beat(text=text, speaker=sp, voice=voices.a if sp == "a" else voices.b))

    if len(beats) < 6:
        raise ValueError(f"Only {len(beats)} usable beats")
    if not any(b.speaker == "b" for b in beats):
        raise ValueError("The student never speaks - that is a monologue, not this format")
    if beats[0].speaker != "a":
        raise ValueError("The expert must open; the first line is the hook")

    hook = strip_emphasis(str(data.get("hook") or beats[0].text)).strip()
    tags = data.get("tags") or ["learnontiktok", "facts", "explained", "fyp"]
    caption = build_caption("explainer", hook, tags)

    return Script(
        beats=beats,
        title=str(data["title"])[:120],
        hook=hook,
        tags=tags,
        caption=caption,
        premise=str(data["premise"]),
    )
