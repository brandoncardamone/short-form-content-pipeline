"""
Kokoro-82M TTS engine. Runs on CPU, no GPU required.

Voice ids follow [a|b][f|m]_name: a=American, b=British, f/m=gender.
Configured voices: af_bella (speaker a), am_adam (speaker b).
"""

from pathlib import Path

import numpy as np
import soundfile as sf

from src.tts.base import TTSEngine


class KokoroEngine(TTSEngine):
    def __init__(self):
        try:
            from kokoro import KPipeline
        except ImportError as e:
            raise ImportError(
                "kokoro is not installed. Run: pip install kokoro\n"
                "If torch has no Python 3.14 wheel, set tts.engine=piper in config.yaml."
            ) from e
        # lang_code='a' covers all American English voices; 'b' for British
        self._pipelines: dict[str, object] = {}

    def _get_pipeline(self, voice: str):
        from kokoro import KPipeline
        lang_code = voice[0]  # 'a' or 'b'
        if lang_code not in self._pipelines:
            self._pipelines[lang_code] = KPipeline(lang_code=lang_code)
        return self._pipelines[lang_code]

    def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        pipeline = self._get_pipeline(voice)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        samples = []
        sample_rate = 24000
        for _, _, audio in pipeline(text, voice=voice, speed=1.0):
            samples.append(audio)

        audio = np.concatenate(samples) if samples else np.zeros(sample_rate // 10)
        sf.write(str(out_path), audio, sample_rate)
