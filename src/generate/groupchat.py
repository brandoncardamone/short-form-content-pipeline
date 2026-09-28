"""
Group-chat script generator - the same iMessage renderer as textchain, but
three or four participants instead of two.

Different rhythm from a one-to-one confrontation: people take sides, someone
goes quiet, someone screenshots. The renderer shows a sender name above the
first bubble of each run (see cards.py `_prepare`), which is what keeps it
readable.

Voice mapping is a real constraint, not a choice: only two reference voices
exist (assets/voices/chatterbox_ref_{female,male}.wav), so the protagonist gets
one and everyone else shares the other. The on-screen sender labels carry
identity instead. Adding a third reference clip would let this map properly.
"""

import json
import logging
import random
from typing import Optional

from src.schema import Script, Beat
from src.generate.llm import load_client, extract_json
from src.captions import build_caption
from src.db import premise_exists

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
SECONDS_PER_BEAT = 1.38   # same measured pacing as textchain

SETTINGS = [
    ("the family group chat", "siblings and a parent, an old grievance resurfacing"),
    ("a friend group chat", "one friend has done something the others are only now piecing together"),
    ("a work group chat", "colleagues realising their manager has misled them"),
    ("a wedding party chat", "a bridesmaid or groomsman discovers something days before"),
    ("a housemate chat", "money, a missing item, and an obvious lie"),
    ("a group chat one person was just added to", "the others forgot they could now see everything"),
    ("a school parents chat", "a small incident escalating far past its size"),
    ("a holiday planning chat", "someone has quietly cancelled or changed something"),
]

EXCLUDED = [
    "graphic violence",
    "sexual content",
    "self-harm or suicide",
    "content involving minors in any charged context",
    "hate speech or slurs",
]

PROMPT = """You write short-form social media scripts formatted as a GROUP iMessage conversation - the
kind people watch to the end and then argue about in the comments.

Setting for this script: {setting} - {situation}

Participants:
- Speaker "a" is the protagonist. Their messages appear on the right in blue, with no name shown.
- Speakers "b", "c" and "d" are the others. Each gets a name shown above their messages.
- Use exactly {n_people} other participants (so speakers b{maybe_c}{maybe_d}).

Rules:
- Realistic texting: mostly lowercase, missing apostrophes, fragments, typos, abbreviations.
- SHORT and FAST. Most messages 2-6 words. A few longer ones (10-14 words) for the big beats only.
- It is a GROUP: people talk over each other, two people side against one, someone stays silent
  for a stretch and then drops the worst message, someone says "why is nobody answering".
- Fragment reveals across consecutive bubbles from the same person rather than one long message.
- Every message moves it forward. No small talk, no throat-clearing.
- Specific, concrete details - names, places, amounts, times.
- Strictly excluded: {excluded}

Structure:
- Message 1 is the hook: drop the viewer mid-confrontation. Do NOT open with "why is/why did" -
  that opening is overused and banned here. Do not open by greeting anyone.
- Rapid escalation, each reveal making the reader go "wait, what".
- Around 60% through it looks like it settles. Then the real thing lands.
- The final message is an in-character call to action, e.g. someone texting
  "comment [word from the story] if you'd tell her" - written as another message, not narration.

Output valid JSON only, no markdown fences:
{{
  "title": "short internal title",
  "group_name": "what this chat is saved as, e.g. 'the cousins 💀' - short, with at most one emoji",
  "participants": {{"b": "Name", "c": "Name"{maybe_d_json}}},
  "hook": "the first message's text",
  "premise": "one-line summary for deduplication",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "beats": [{{"speaker": "a", "text": "message"}}, ...]
}}

Produce exactly {n_messages} messages, the last being the in-character call to action."""


def _target_messages(cfg) -> int:
    mid = (cfg.video.target_duration_min + cfg.video.target_duration_max) / 2
    return max(12, round(mid / SECONDS_PER_BEAT))


def generate_groupchat_script(cfg, db_conn) -> Script:
    client = load_client(cfg)
    n = _target_messages(cfg)
    temperature = 0.95

    for attempt in range(1, MAX_RETRIES + 1):
        setting, situation = random.choice(SETTINGS)
        n_people = random.choice([2, 2, 3])   # b+c, or b+c+d
        logger.info("Group chat: setting=%r others=%d n=%d", setting, n_people, n)

        prompt = PROMPT.format(
            setting=setting, situation=situation, n_messages=n,
            n_people=n_people,
            maybe_c=" and c" if n_people >= 2 else "",
            maybe_d=" and d" if n_people >= 3 else "",
            maybe_d_json=', "d": "Name"' if n_people >= 3 else "",
            excluded=", ".join(EXCLUDED),
        )
        raw = client.complete(prompt, temperature=temperature)

        try:
            script = _parse(extract_json(raw), cfg)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("Group chat parse failed (attempt %d): %s", attempt, e)
            if attempt == MAX_RETRIES:
                raise
            temperature = min(temperature + 0.1, 1.4)
            continue

        if db_conn is not None and premise_exists(db_conn, script.premise):
            logger.warning("Duplicate premise (attempt %d): %s", attempt, script.premise)
            if attempt == MAX_RETRIES:
                raise ValueError("Could not generate a non-duplicate group chat")
            temperature = min(temperature + 0.15, 1.5)
            continue

        logger.info("Group chat accepted: %r (%d beats, %d others)",
                    script.title, len(script.beats), len(script.participants or {}))
        return script

    raise RuntimeError("Group chat generation loop exited without returning")


def _parse(data: dict, cfg) -> Script:
    voices = cfg.tts.voices
    participants = {
        str(k).lower().strip(): str(v).strip()
        for k, v in (data.get("participants") or {}).items()
        if str(k).lower().strip() in ("b", "c", "d", "e") and str(v).strip()
    }
    if not participants:
        raise ValueError("No named participants")

    beats = []
    for i, b in enumerate(data["beats"]):
        sp = str(b["speaker"]).lower().strip()
        if sp not in ("a",) and sp not in participants:
            raise ValueError(f"Beat {i} has speaker {sp!r} with no participant name")
        beats.append(Beat(
            text=b["text"],
            speaker=sp,
            # Only two reference voices exist; the protagonist gets one and
            # everyone else shares the other. Sender labels carry identity.
            voice=voices.a if sp == "a" else voices.b,
        ))
    if not beats:
        raise ValueError("No beats in generated script")
    if not any(b.speaker == "a" for b in beats):
        raise ValueError("Protagonist never speaks")

    hook = data.get("hook") or beats[0].text
    tags = data.get("tags", [])
    caption = build_caption("textchain", hook, tags)

    group_name = str(data.get("group_name") or "").strip()
    if not group_name or len(group_name) > 24:
        group_name = random.choice(["the group 👀", "the cousins", "work 🙃", "the house"])

    return Script(
        beats=beats,
        title=data["title"],
        hook=hook,
        tags=tags,
        caption=caption,
        premise=data["premise"],
        contact_name=group_name,
        participants=participants,
    )
