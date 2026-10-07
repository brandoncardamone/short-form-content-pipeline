"""
Sanity checks for one generated beat of speech.

Chatterbox is a sampling model, and now and then a sample goes wrong in a way
that is obvious to a listener and invisible to the pipeline: a stretch of
gibberish, a word repeated or swallowed, a long dead pause, a burst of noise
after the sentence ends. The more expressive the settings, the more often it
happens. Nothing downstream can tell - the file is a valid WAV of plausible
length - so this is the only place it can be caught.

Four independent checks, cheapest first. A beat must pass all of them:

  energy     the clip is not (near) silent
  clipping   it is not hard-clipped
  duration   its length is plausible for the number of words
  silence    it has no long dead gap in the middle
  transcript a speech recogniser, given only the audio, hears the words that
             were supposed to be said

The transcript check is the one that catches "inhuman" audio, since garbled
speech does not transcribe back to its script. It uses Whisper through
`transformers`, which Chatterbox already depends on, so it adds no package.
If the recogniser cannot be loaded the other checks still run: this module
must never be the reason a video fails to build.

run_tts regenerates a beat that fails (sampling again gives a different
take) and keeps the best take if none passes.
"""

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

ASR_MODEL = "openai/whisper-base.en"

# Seconds of raw (pre-tempo) speech per word. Normal reads measure ~0.28;
# these bounds are deliberately wide and only catch the pathological takes.
MIN_S_PER_WORD = 0.11
MAX_S_PER_WORD = 0.80
DURATION_SLACK_S = 0.9

MAX_INTERNAL_SILENCE_S = 0.9
SILENCE_DBFS = -42.0
MIN_RMS_DBFS = -38.0
MAX_CLIPPED_FRACTION = 0.002

# Similarity between what was scripted and what the recogniser heard, 0-1.
# Calibrated on real takes: clean reads score 0.9+, and game vocabulary the
# recogniser has never seen ("Shimmer", "Aether") costs a little, so the bar
# sits well below a perfect match.
MIN_TRANSCRIPT_MATCH = 0.72
MAX_EXTRA_WORDS_RATIO = 1.6   # babble: far more words heard than scripted

_asr = None
_asr_failed = False


@dataclass
class Verdict:
    ok: bool
    score: float                       # higher is better; used to pick the best failed take
    problems: list[str] = field(default_factory=list)
    heard: Optional[str] = None
    duration_s: float = 0.0

    def __str__(self) -> str:
        return "ok" if self.ok else "; ".join(self.problems)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", text.lower())).strip()


def _load_asr():
    global _asr, _asr_failed
    if _asr is None and not _asr_failed:
        try:
            from transformers import pipeline
            from transformers.utils import logging as hf_logging
            hf_logging.disable_progress_bar()   # hundreds of lines per load otherwise
            _asr = pipeline("automatic-speech-recognition", model=ASR_MODEL, device="cpu")
        except Exception as e:      # missing model, no network, API change: fail open
            _asr_failed = True
            logger.warning("Speech recogniser unavailable - transcript check disabled: %s", e)
    return _asr


def _transcribe(audio: np.ndarray, sr: int) -> Optional[str]:
    asr = _load_asr()
    if asr is None:
        return None
    try:
        if sr != 16000:
            import librosa
            audio = librosa.resample(audio.astype(np.float32), orig_sr=sr, target_sr=16000)
            sr = 16000
        return asr({"raw": audio.astype(np.float32), "sampling_rate": sr})["text"]
    except Exception as e:
        logger.warning("Transcription failed - skipping the transcript check for this beat: %s", e)
        return None


def _longest_internal_silence(audio: np.ndarray, sr: int) -> float:
    """Longest quiet run strictly inside the speech (leading/trailing excluded)."""
    win = max(1, int(sr * 0.02))
    n = len(audio) // win
    if n < 3:
        return 0.0
    rms = np.sqrt(np.mean(audio[: n * win].reshape(n, win) ** 2, axis=1) + 1e-12)
    quiet = 20 * np.log10(rms) < SILENCE_DBFS
    loud = np.flatnonzero(~quiet)
    if len(loud) < 2:
        return 0.0
    longest = run = 0
    for q in quiet[loud[0]: loud[-1] + 1]:
        run = run + 1 if q else 0
        longest = max(longest, run)
    return longest * win / sr


def check(wav_path: Path, text: str, transcript: bool = True) -> Verdict:
    """Judge one raw (pre-tempo) beat against the text it should contain."""
    audio, sr = sf.read(str(wav_path), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    duration = len(audio) / sr
    words = max(1, len(_norm(text).split()))
    problems: list[str] = []
    score = 1.0

    rms_db = 20 * np.log10(np.sqrt(np.mean(audio ** 2) + 1e-12))
    if rms_db < MIN_RMS_DBFS:
        problems.append(f"near-silent ({rms_db:.0f} dBFS)")
        score -= 1.0

    clipped = float(np.mean(np.abs(audio) > 0.99))
    if clipped > MAX_CLIPPED_FRACTION:
        problems.append(f"clipped ({clipped:.1%} of samples)")
        score -= 0.5

    lo, hi = words * MIN_S_PER_WORD, words * MAX_S_PER_WORD + DURATION_SLACK_S
    if not lo <= duration <= hi:
        problems.append(f"duration {duration:.1f}s implausible for {words} words "
                        f"(expected {lo:.1f}-{hi:.1f}s)")
        score -= 0.5

    gap = _longest_internal_silence(audio, sr)
    if gap > MAX_INTERNAL_SILENCE_S:
        problems.append(f"{gap:.1f}s dead gap mid-sentence")
        score -= 0.3

    heard = _transcribe(audio, sr) if transcript else None
    if heard is not None:
        want, got = _norm(text), _norm(heard)
        match = SequenceMatcher(None, want, got).ratio()
        score += match - 1.0
        if match < MIN_TRANSCRIPT_MATCH:
            problems.append(f"transcript match {match:.2f}: heard {heard.strip()!r}")
        elif len(got.split()) > words * MAX_EXTRA_WORDS_RATIO + 2:
            problems.append(f"extra speech: heard {len(got.split())} words for {words}")
            score -= 0.3

    return Verdict(ok=not problems, score=score, problems=problems, heard=heard,
                   duration_s=duration)
