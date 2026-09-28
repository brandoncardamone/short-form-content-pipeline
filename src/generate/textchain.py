"""
Text-chain script generator. Calls the LLM with a structured prompt and returns
a validated Script. Checks premise_hash against the DB before accepting.

Message count is derived from video.target_duration_* (see _target_messages).
Theme and hook shape are randomised per script to stop the model collapsing
onto one template.
"""

import hashlib
import json
import logging
from typing import Optional

from src.schema import Script, Beat
from src.generate.llm import load_client, extract_json
from src.captions import build_caption
from src.db import premise_exists, premise_hash

logger = logging.getLogger(__name__)

# One theme and one hook shape are drawn at random per generation. Left to its
# own devices the model collapses onto a single template: of 27 published
# text chains, 25 opened with the literal word "why" (18 of them "why is...").
# Every video announced itself as the same video in under a second, which is
# exactly the window a viewer uses to decide whether to keep watching.
THEMES = [
    "relationship drama",
    "family secrets",
    "workplace conflict",
    "mild suspense / unexpected revelation",
    "friendship betrayal",
    "neighbours, landlords and small-town gossip",
    "money, wills and inheritance disputes",
    "roommates and living situations",
    "an old secret resurfacing years later",
    "a message that reached the wrong person",
    "a favour that turns out to have strings attached",
    "someone being quietly replaced - at work, in a family, in a friendship",
]

HOOK_SHAPES = [
    "a flat statement of a disturbing fact, with no question anywhere in it",
    "someone answering a question the viewer never saw asked",
    "a single word on its own, then the reveal across the next two bubbles",
    "an apology for something that has not been explained yet",
    "someone who has clearly texted the wrong person and already knows it",
    "a demand to know where the other person is, right now",
    "someone calmly describing something they are looking at this second",
    "a threat phrased politely",
    "an accusation delivered as a compliment",
    "someone reading aloud from something they just found",
    "a reply to a photo the viewer cannot see",
    "someone insisting nothing is wrong while contradicting themselves",
    "a correction of a small detail that should not matter, but clearly does",
    "someone thanking the other person for something sinister",
]

EXCLUDED = [
    "graphic violence",
    "sexual content",
    "self-harm or suicide",
    "content involving minors in any charged context",
    "hate speech or slurs",
]

SYSTEM_PROMPT = """You write short-form social media scripts formatted as iMessage chat conversations, in
the style of the highest-performing "text storytime" videos on TikTok/Reels — the ones people
watch to the end and comment on.

Rules for message content:
- Realistic texting register: mostly lowercase, missing apostrophes, fragments, typos, abbreviations
- Alternating but uneven: speakers send 1-3 messages in a row, not strict back-and-forth
- Keep it SHORT and FAST. Most messages are 2-6 words. A short handful of longer ones (10-14 words)
  for the biggest emotional beats only — everything else should be readable almost instantly.
- Fragment big reveals across consecutive bubbles from the same speaker instead of one long message.
  E.g. instead of "you're telling me amy the hottest girl in school wants me" write three short
  bubbles in a row: "you're telling me" / "AMY" / "the hottest girl in school wants me". This staccato
  rhythm across bubbles is a core technique of this format, not just a stylistic option.
- Tension doesn't only come from new facts — repeat a contradiction or push-pull with small variations
  (e.g. one speaker alternates between pushing the other away and calling them pet names) rather than
  purely stacking new escalating reveals every single line.
- Every message must move the story forward — cut anything a viewer would scroll past. No small talk,
  no throat-clearing, no restating what was just said.
- Use specific, concrete, sensory details (names, places, objects, times) instead of vague ones —
  specificity is what makes it feel real and screenshottable, not generic.
- Theme for THIS script (commit to it, do not drift into the others): {themes}
- Strictly excluded: {excluded}

Structure (this pacing is what makes these videos work — do not soften it):
- Message 1: the hook. A single line so alarming, confusing, or specific that someone scrolling
  would stop. Never a lead-in like "hey we need to talk" — start already inside the confrontation.
  THIS SCRIPT'S HOOK MUST TAKE THIS SHAPE: {hook_shape}
  Do NOT open with "why is", "why did", "why does", or any other "why ..." question. That opening
  is heavily overused and instantly recognisable as formulaic — it is banned for this script.
  Do not open with the other party's name either.
- Next messages: rapid-fire escalation. Each reveal should make the reader go "wait, what."
- ~55-65% through: a false resolution or a moment it seems like it might be fine — brief, then broken.
- Final 3-4 messages: the real twist, worse than expected. It's fine to leave the twist implicit
  (trust subtext over spelling it out) rather than stating the conclusion outright.
- The very last beat: an in-character call-to-action line that fits inside the fictional conversation
  itself, e.g. one party literally texting something like "comment [name/word from the story] for
  part 2" — a hook for a sequel, delivered as if it were just another text message, not narration.

Output valid JSON only, no markdown fences. Schema:
{{
  "title": "short internal title",
  "hook": "one-sentence hook (same as first message text)",
  "premise": "one-line summary for deduplication",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "beats": [
    {{"speaker": "a", "text": "message text"}},
    ...
  ]
}}

Speaker A is the protagonist (sends first). Speaker B is the other party.
Produce exactly {n_messages} messages, the last of which is the in-character call-to-action line."""

MAX_RETRIES = 3

# Measured ~1.38s per message with Chatterbox at the configured speed/gap and
# this prompt's short-message style (a 34-beat run came out at 47.0s). Derived
# from the configured target rather than hardcoded, so changing
# video.target_duration_* actually changes the script length instead of
# silently producing scripts for the old target.
SECONDS_PER_BEAT = 1.38


def _target_messages(cfg) -> int:
    mid = (cfg.video.target_duration_min + cfg.video.target_duration_max) / 2
    return max(12, round(mid / SECONDS_PER_BEAT))


def generate_script(cfg, db_conn, n_messages: Optional[int] = None) -> Script:
    """Generate a Script and verify it isn't a duplicate. Retries up to MAX_RETRIES times."""
    import random

    client = load_client(cfg)
    n = n_messages or _target_messages(cfg)

    theme = random.choice(THEMES)
    hook_shape = random.choice(HOOK_SHAPES)
    logger.info("Text chain: theme=%r hook shape=%r n=%d", theme, hook_shape, n)

    prompt = SYSTEM_PROMPT.format(
        themes=theme,
        excluded=", ".join(EXCLUDED),
        n_messages=n,
        hook_shape=hook_shape,
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

    # Caption is rebuilt fresh at publish time too (see src/captions.py +
    # cli.py's _publish_row) — this generation-time value is just what gets
    # stored initially, not relied on as the final word.
    caption = build_caption("textchain", data.get("hook") or data["title"], data.get("tags", []))

    return Script(
        beats=beats,
        title=data["title"],
        hook=data["hook"],
        tags=data.get("tags", []),
        caption=caption,
        premise=data["premise"],
    )
