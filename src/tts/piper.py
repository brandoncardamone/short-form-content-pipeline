"""
Piper TTS engine. ONNX-based, no torch dependency — works on Python 3.14.

Voice models must be downloaded before first use. Run:
  python -m src.tts.piper --download

Recommended voices:
  Speaker a (female): en_US-lessac-medium
  Speaker b (male):   en_US-ryan-medium
"""

import os
import urllib.request
import wave
from pathlib import Path

from src.tts.base import TTSEngine

VOICES_DIR = Path(__file__).parent.parent.parent / "assets" / "voices"
HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

VOICE_MAP = {
    "af_bella": "en/en_US/lessac/medium/en_US-lessac-medium",
    "am_adam":  "en/en_US/ryan/medium/en_US-ryan-medium",
}


def voice_paths(voice_id: str) -> tuple[Path, Path]:
    stem = VOICE_MAP.get(voice_id)
    if not stem:
        raise ValueError(
            f"Unknown piper voice id {voice_id!r}. "
            f"Add it to VOICE_MAP in src/tts/piper.py."
        )
    name = stem.split("/")[-1]
    return (
        VOICES_DIR / f"{name}.onnx",
        VOICES_DIR / f"{name}.onnx.json",
    )


def download_voice(voice_id: str) -> None:
    stem = VOICE_MAP[voice_id]
    name = stem.split("/")[-1]
    VOICES_DIR.mkdir(parents=True, exist_ok=True)

    for ext in (".onnx", ".onnx.json"):
        dest = VOICES_DIR / f"{name}{ext}"
        if dest.exists():
            print(f"  already have {dest.name}")
            continue
        url = f"{HF_BASE}/{stem}{ext}"
        print(f"  downloading {dest.name} …")
        urllib.request.urlretrieve(url, dest)
        print(f"  saved {dest}")


class PiperEngine(TTSEngine):
    def __init__(self):
        try:
            import piper as _piper
            self._piper = _piper
        except ImportError as e:
            raise ImportError(
                "piper-tts is not installed. Run: pip install piper-tts"
            ) from e
        self._cache: dict[str, object] = {}

    def _get_voice(self, voice_id: str):
        if voice_id not in self._cache:
            onnx, cfg = voice_paths(voice_id)
            if not onnx.exists():
                raise FileNotFoundError(
                    f"Voice model not found: {onnx}. "
                    f"Run: python -m src.tts.piper --download"
                )
            self._cache[voice_id] = self._piper.PiperVoice.load(
                str(onnx), config_path=str(cfg), use_cuda=False
            )
        return self._cache[voice_id]

    def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        pv = self._get_voice(voice)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as wf:
            pv.synthesize_wav(text, wf)


if __name__ == "__main__":
    import sys
    if "--download" in sys.argv:
        for vid in VOICE_MAP:
            print(f"Downloading voice: {vid}")
            download_voice(vid)
        print("Done.")
    else:
        print("Usage: python -m src.tts.piper --download")
