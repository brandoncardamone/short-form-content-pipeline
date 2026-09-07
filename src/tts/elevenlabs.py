"""
ElevenLabs TTS engine. Free-tier text-to-speech via the ElevenLabs REST API.

Voice selection is a one-time human/AI decision, not a per-request one: run
  python -m src.tts.elevenlabs --list-voices
to see premade voices available to your account, then put the chosen voice_ids
in VOICE_MAP below (or override via config.yaml tts.voices).

ElevenLabs returns MP3; we convert to WAV via ffmpeg since the rest of the
pipeline (duration measurement, atempo, ffmpeg assembly) expects WAV.
"""

import os
import subprocess
from pathlib import Path

import requests

from src.tts.base import TTSEngine

API_BASE = "https://api.elevenlabs.io/v1"
MODEL_ID = "eleven_multilingual_v2"

# Chosen from `--list-voices` output for this account.
VOICE_MAP = {
    "voice_a": "cgSgspJ2msm6clMCkdW9",  # Jessica — playful, bright, warm (female)
    "voice_b": "TX3LPaxmHKxFdv7VOQHJ",  # Liam — energetic, social media creator (male)
}


def list_voices(api_key: str) -> list[dict]:
    resp = requests.get(
        f"{API_BASE}/voices",
        headers={"xi-api-key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["voices"]


class ElevenLabsEngine(TTSEngine):
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        if not self.api_key:
            raise ValueError(
                "ELEVENLABS_API_KEY is not set. Add it to .env."
            )

    def _voice_id(self, voice: str) -> str:
        voice_id = VOICE_MAP.get(voice, voice)  # allow raw voice_id passthrough
        if not voice_id:
            raise ValueError(
                f"No ElevenLabs voice_id configured for {voice!r}. "
                f"Run `python -m src.tts.elevenlabs --list-voices` and fill in VOICE_MAP."
            )
        return voice_id

    def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        voice_id = self._voice_id(voice)
        resp = requests.post(
            f"{API_BASE}/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": self.api_key,
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": MODEL_ID,
                # Lower stability + higher style push the model toward more
                # expressive, varied delivery instead of a flat/deadpan read.
                "voice_settings": {
                    "stability": 0.32,
                    "similarity_boost": 0.8,
                    "style": 0.45,
                    "use_speaker_boost": True,
                },
            },
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"ElevenLabs TTS failed ({resp.status_code}): {resp.text[:300]}"
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        mp3_path = out_path.with_suffix(".mp3")
        mp3_path.write_bytes(resp.content)

        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path), str(out_path)],
            check=True,
            capture_output=True,
        )
        mp3_path.unlink()


if __name__ == "__main__":
    import sys

    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        from dotenv import load_dotenv
        load_dotenv()
        key = os.getenv("ELEVENLABS_API_KEY")

    if "--list-voices" in sys.argv:
        if not key:
            print("ELEVENLABS_API_KEY not set.")
            sys.exit(1)
        for v in list_voices(key):
            labels = v.get("labels", {})
            print(f"{v['voice_id']}  {v['name']!r:25} gender={labels.get('gender')} accent={labels.get('accent')} category={v.get('category')}")
    else:
        print("Usage: python -m src.tts.elevenlabs --list-voices")
