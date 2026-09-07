from pathlib import Path
from typing import Optional
from pydantic import BaseModel


class Beat(BaseModel):
    text: str
    speaker: str   # "a" or "b"; single-narrator content uses "a" throughout
    voice: str     # TTS voice id


class RedditCardMeta(BaseModel):
    """
    Per-beat render metadata for the reddit_story content format. One entry
    per beat, same length/order as Script.beats. Beat.text stays the plain
    narration text for TTS; this carries what the card should show alongside it.
    """
    kind: str                        # "post" | "comment"
    is_first_of_unit: bool           # show header/title only on the first chunk of a post/comment
    subreddit: str
    username: str
    timestamp: str                   # precomputed relative time, e.g. "3y"
    awards: int = 0
    is_nsfw: bool = False
    title: Optional[str] = None      # post title; only set when kind="post" and is_first_of_unit


class Script(BaseModel):
    beats: list[Beat]
    title: str
    hook: str
    tags: list[str]
    caption: str
    premise: str   # one-line summary, used for dedup
    card_meta: Optional[list[RedditCardMeta]] = None   # reddit_story format only


class RenderedBeat(BaseModel):
    """
    Timing contract between TTS and downstream stages.
    Both the frame renderer and audio assembler consume this list.
    Duration is always measured from the WAV file, never estimated.
    """
    index: int
    wav_path: Path
    duration_ms: float   # measured from WAV via soundfile
    gap_ms: float        # silence appended after this beat
