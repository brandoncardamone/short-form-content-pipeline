from pathlib import Path
from pydantic import BaseModel


class Beat(BaseModel):
    text: str
    speaker: str   # "a" or "b"; single-narrator content uses "a" throughout
    voice: str     # TTS voice id


class Script(BaseModel):
    beats: list[Beat]
    title: str
    hook: str
    tags: list[str]
    caption: str
    premise: str   # one-line summary, used for dedup


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
