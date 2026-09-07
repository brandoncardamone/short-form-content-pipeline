"""
Chatterbox TTS engine (resemble-ai/chatterbox). Open-weight, free, runs locally
on CPU — no API key, no quota. Chosen as the free replacement for ElevenLabs
after its free-tier quota was exhausted.

Unlike ElevenLabs/Piper, Chatterbox has no voice-ID library: it clones a voice
from a short reference clip (`audio_prompt_path`) at generation time. The
production reference clips live in assets/voices/chatterbox_ref_{female,male}.wav
(finalized 2026-09-01):
  - female: 57.5s, stitched from 15 short clips pulled from a two-speaker open
    audio sample the user provided ("female and male 3.mp3"), each verified
    frame-by-frame via pitch analysis to contain zero male-range F0 content —
    naive coarse-average checks let 25-50% male contamination slip through in
    earlier attempts, which is what caused prior "sounds robotic / male mixed
    in" complaints.
  - male: 120s, from a different open audio sample the user provided
    ("download.mp4"), explicitly an AI-generated voice shared for public use.
  Do not regenerate these from Piper output — Chatterbox reproduces the
  reference's own audio quality along with its voice, and a synthetic/lossy
  source measurably degrades the clone (see chatterbox docs cited in memory).

CPU-only inference is slow: ~13-16s per short sentence on this machine, no
GPU available. A full 34-44 beat video's TTS stage can take on the order of
10-20 minutes. This is a known, accepted tradeoff for free + high quality.

Requires: pip install chatterbox-tts, and setuptools<81 (chatterbox's `perth`
watermarking dependency imports pkg_resources, which setuptools>=81 removed).
"""

from pathlib import Path

import soundfile as sf

from src.tts.base import TTSEngine

VOICES_DIR = Path(__file__).parent.parent.parent / "assets" / "voices"

VOICE_MAP = {
    "voice_a": VOICES_DIR / "chatterbox_ref_female.wav",
    "voice_b": VOICES_DIR / "chatterbox_ref_male.wav",
}

# Higher exaggeration pushes delivery away from Chatterbox's default flat/calm
# read, toward the more animated register these videos need. Lower cfg_weight
# trades a little reference-voice adherence for more expressive variation.
EXAGGERATION = 0.7
CFG_WEIGHT = 0.45


class ChatterboxEngine(TTSEngine):
    def __init__(self):
        from chatterbox.tts import ChatterboxTTS
        self._model = ChatterboxTTS.from_pretrained(device="cpu")

    def _ref_path(self, voice: str) -> Path:
        ref = VOICE_MAP.get(voice)
        if ref is None:
            raise ValueError(
                f"No Chatterbox reference clip configured for {voice!r}. "
                f"Add it to VOICE_MAP in src/tts/chatterbox.py."
            )
        if not Path(ref).exists():
            raise FileNotFoundError(f"Chatterbox reference clip not found: {ref}")
        return Path(ref)

    def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        ref_path = self._ref_path(voice)
        wav = self._model.generate(
            text,
            audio_prompt_path=str(ref_path),
            exaggeration=EXAGGERATION,
            cfg_weight=CFG_WEIGHT,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out_path), wav.squeeze(0).numpy(), self._model.sr)
