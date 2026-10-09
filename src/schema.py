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


class BeatVisual(BaseModel):
    """
    What the terraria format shows while a beat is narrated. Only the URL is
    stored - the renderer downloads it - so a stored script stays renderable
    on a machine that never ran the generate stage.
    """
    url: str
    label: str = ""                  # shown under the image, e.g. "True Night's Edge"
    width: int = 0                   # source pixel size, before upscaling
    height: int = 0
    animated: bool = False           # a GIF, played back in sync with the video


class Script(BaseModel):
    beats: list[Beat]
    title: str
    hook: str
    tags: list[str]
    caption: str
    premise: str   # one-line summary, used for dedup
    card_meta: Optional[list[RedditCardMeta]] = None   # reddit_story format only
    # textchain only: who the conversation is WITH, as it would appear at the
    # top of a phone. Comes from the generated script so it matches the story;
    # falls back to config when absent (older rows predate this field).
    contact_name: Optional[str] = None
    # groupchat only: {"b": "Dana", "c": "Priya"} - the name shown above each
    # non-protagonist speaker's first bubble in a run.
    participants: Optional[dict[str, str]] = None
    # terraria only: one entry per beat, same length/order as beats. None means
    # "keep showing the previous beat's image".
    visuals: Optional[list[Optional[BeatVisual]]] = None
    source_url: Optional[str] = None   # terraria only: the wiki article used
    # terraria only: a few words shown huge on the opening title card, which is
    # also the frame used as the reel's cover. May carry one *emphasis* word.
    cover_line: Optional[str] = None


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
