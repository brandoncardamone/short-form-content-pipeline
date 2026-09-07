"""
TTS provider interface. All engines implement TTSEngine.
Engine selection is a config value (tts.engine: kokoro | piper).
"""

from abc import ABC, abstractmethod
from pathlib import Path
import sys

import soundfile as sf


class TTSEngine(ABC):
    @abstractmethod
    def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        """Render text to a WAV file at out_path."""

    def duration_ms(self, wav_path: Path) -> float:
        info = sf.info(str(wav_path))
        return info.duration * 1000.0


def load_engine(engine_name: str) -> "TTSEngine":
    if engine_name == "kokoro":
        from src.tts.kokoro import KokoroEngine
        return KokoroEngine()
    elif engine_name == "piper":
        from src.tts.piper import PiperEngine
        return PiperEngine()
    elif engine_name == "elevenlabs":
        from src.tts.elevenlabs import ElevenLabsEngine
        return ElevenLabsEngine()
    elif engine_name == "chatterbox":
        from src.tts.chatterbox import ChatterboxEngine
        return ChatterboxEngine()
    else:
        raise ValueError(f"Unknown TTS engine: {engine_name!r}. Choose 'kokoro', 'piper', 'elevenlabs', or 'chatterbox'.")
