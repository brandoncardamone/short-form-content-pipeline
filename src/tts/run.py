"""
TTS stage. Synthesizes one WAV per beat, applies optional speed-up via
ffmpeg atempo, measures duration from the output file, and emits RenderedBeat.

Speed is applied after synthesis so the TTS engine always runs at 1.0x — this
keeps the engine interface simple and gives ffmpeg full quality control over
the tempo change.
"""

import logging
import random
import subprocess
from pathlib import Path

from src.schema import Script, RenderedBeat
from src.config import Config
from src.tts.base import load_engine

logger = logging.getLogger(__name__)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def pick_delivery(cfg) -> dict:
    """Randomise the read once per video.

    Speed, expressiveness and reference-adherence all move together within
    configured jitter bands. Bounds are hard-clamped: atempo below ~0.8 sounds
    sluggish and above ~1.6 starts chewing consonants, and Chatterbox degrades
    outside roughly 0.3-1.0 on both of its knobs.

    Note this is NOT pitch shifting. Post-processing pitch shifts were tried
    (both naive and formant-preserving) and made quality measurably worse - see
    HANDOFF.md. Varying the generation parameters changes the performance
    rather than resampling the output, which is why it does not degrade.
    """
    t = cfg.tts
    return {
        "speed": round(_clamp(random.uniform(t.speed - t.speed_jitter,
                                             t.speed + t.speed_jitter), 0.8, 1.6), 3),
        "exaggeration": round(_clamp(random.uniform(t.exaggeration - t.exaggeration_jitter,
                                                    t.exaggeration + t.exaggeration_jitter),
                                     0.3, 1.0), 3),
        "cfg_weight": round(_clamp(random.uniform(t.cfg_weight - t.cfg_weight_jitter,
                                                  t.cfg_weight + t.cfg_weight_jitter),
                                   0.2, 0.9), 3),
    }


def run_tts(script: Script, work_dir: Path, cfg: Config) -> list[RenderedBeat]:
    engine = load_engine(cfg.tts.engine)
    delivery = pick_delivery(cfg)
    engine.set_delivery(delivery)
    logger.info("Delivery for this video: speed=%.3f exaggeration=%.3f cfg_weight=%.3f",
                delivery["speed"], delivery["exaggeration"], delivery["cfg_weight"])

    work_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[RenderedBeat] = []
    retakes = unresolved = 0

    for i, beat in enumerate(script.beats):
        raw_path = work_dir / f"beat_{i:03d}_raw.wav"
        final_path = work_dir / f"beat_{i:03d}.wav"

        if cfg.tts.qa_enabled:
            used, passed = _synthesize_checked(engine, beat, raw_path, delivery, cfg, i)
            retakes += used - 1
            unresolved += not passed
        else:
            engine.synthesize(beat.text, beat.voice, raw_path)

        if abs(delivery["speed"] - 1.0) > 0.01:
            _apply_tempo(raw_path, final_path, delivery["speed"])
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

    if cfg.tts.qa_enabled:
        logger.info("Voice QA: %d beats, %d retake(s), %d beat(s) kept despite failing every take",
                    len(rendered), retakes, unresolved)
    return rendered


# Delivery used for the final retake of a beat that keeps failing: Chatterbox's
# calm defaults, which glitch least. One beat read slightly flatter is far less
# noticeable than one beat of garbled audio.
SAFE_DELIVERY = {"exaggeration": 0.5, "cfg_weight": 0.5}


def _synthesize_checked(engine, beat, raw_path: Path, delivery: dict, cfg, index: int) -> tuple[int, bool]:
    """Synthesize one beat, re-sampling it until it passes src.tts.qa.

    Returns (takes used, whether the kept take passed). Chatterbox samples, so
    a retake is a genuinely different read rather than the same failure again.
    If no take passes, the best-scoring one is kept: a video with one imperfect
    line still beats no video.
    """
    from src.tts import qa

    attempts = max(1, cfg.tts.qa_max_attempts)
    best_score, best_path, best_verdict = None, None, None
    for attempt in range(1, attempts + 1):
        take = raw_path.with_name(f"{raw_path.stem}_take{attempt}.wav")
        if attempt == attempts and attempts > 1:
            engine.set_delivery(SAFE_DELIVERY)
        try:
            engine.synthesize(beat.text, beat.voice, take)
        finally:
            engine.set_delivery(delivery)
        verdict = qa.check(take, beat.text)
        if best_score is None or verdict.score > best_score:
            if best_path is not None:
                best_path.unlink(missing_ok=True)
            best_score, best_path, best_verdict = verdict.score, take, verdict
        else:
            take.unlink(missing_ok=True)
        if verdict.ok:
            break
        logger.warning("Voice QA: beat %d take %d/%d failed (%s)", index, attempt, attempts, verdict)

    best_path.rename(raw_path)
    if not best_verdict.ok:
        logger.warning("Voice QA: beat %d never passed; keeping the best take (%s)",
                       index, best_verdict)
    return attempt, best_verdict.ok


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
