"""
TTS stage. Synthesizes one WAV per beat, applies optional speed-up via
ffmpeg atempo, measures duration from the output file, and emits RenderedBeat.

Speed is applied after synthesis so the TTS engine always runs at 1.0x — this
keeps the engine interface simple and gives ffmpeg full quality control over
the tempo change.
"""

import subprocess
from pathlib import Path

from src.schema import Script, RenderedBeat
from src.config import Config
from src.tts.base import load_engine


def run_tts(script: Script, work_dir: Path, cfg: Config) -> list[RenderedBeat]:
    engine = load_engine(cfg.tts.engine)
    work_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[RenderedBeat] = []

    for i, beat in enumerate(script.beats):
        raw_path = work_dir / f"beat_{i:03d}_raw.wav"
        final_path = work_dir / f"beat_{i:03d}.wav"

        engine.synthesize(beat.text, beat.voice, raw_path)

        if abs(cfg.tts.speed - 1.0) > 0.01:
            _apply_tempo(raw_path, final_path, cfg.tts.speed)
            raw_path.unlink()
        else:
            raw_path.rename(final_path)

        duration_ms = engine.duration_ms(final_path)
        rendered.append(RenderedBeat(
            index=i,
            wav_path=final_path,
            duration_ms=duration_ms,
            gap_ms=cfg.tts.gap_ms,
        ))

    return rendered


def _apply_tempo(src: Path, dst: Path, speed: float) -> None:
    """Apply ffmpeg atempo to change playback speed without pitch shift.
    atempo accepts values 0.5–2.0; chain two filters for values outside that range."""
    if speed <= 2.0:
        atempo = f"atempo={speed:.4f}"
    else:
        # chain: e.g. speed=2.5 → atempo=2.0,atempo=1.25
        atempo = f"atempo=2.0,atempo={speed / 2.0:.4f}"

    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-filter:a", atempo, str(dst)],
        check=True,
        capture_output=True,
    )
