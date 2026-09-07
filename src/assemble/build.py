"""
Video assembly. One ffmpeg pass:
  - Background clip looped to match total duration
  - Card PNG frames overlaid via concat demuxer
  - Voice WAVs concatenated with gap silence, tempo-shifted, loudnorm applied
  - Optional music bed mixed at low volume

After building, asserts the output has the correct duration and both streams.
"""

import json
import logging
import random
import subprocess
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

FRAME_W, FRAME_H = 1080, 1920
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _build_audio(
    rendered_beats,
    work_dir: Path,
    cfg,
) -> Path:
    """
    Concatenate beat WAVs with silence gaps, apply loudnorm.
    Returns path to the finished voice track.
    """
    from src.schema import RenderedBeat

    with wave.open(str(rendered_beats[0].wav_path)) as wf:
        voice_sample_rate = wf.getframerate()

    silence_path = work_dir / "silence.wav"
    _write_silence(silence_path, cfg.tts.gap_ms, sample_rate=voice_sample_rate)

    concat_list = work_dir / "audio_concat.txt"
    lines = []
    for rb in rendered_beats:
        lines.append(f"file '{rb.wav_path.resolve()}'")
        lines.append(f"file '{silence_path.resolve()}'")
    concat_list.write_text("\n".join(lines))

    raw_voice = work_dir / "voice_raw.wav"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy",
            str(raw_voice),
        ],
        check=True, capture_output=True,
    )

    voice_norm = work_dir / "voice.wav"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(raw_voice),
            "-af", LOUDNORM,
            str(voice_norm),
        ],
        check=True, capture_output=True,
    )
    return voice_norm


def _write_silence(path: Path, duration_ms: float, sample_rate: int = 22050) -> None:
    n_frames = int(sample_rate * duration_ms / 1000)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)


def _build_concat(frames_dir: Path, manifest: list[dict]) -> Path:
    """Write ffmpeg concat demuxer file for the card PNG frames."""
    concat = frames_dir / "concat.txt"
    lines = []
    for f in manifest:
        lines.append(f"file '{(frames_dir / f['path']).resolve()}'")
        lines.append(f"duration {f['ms'] / 1000:.4f}")
    # ffmpeg concat demuxer requires the last file to be repeated without duration
    lines.append(f"file '{(frames_dir / manifest[-1]['path']).resolve()}'")
    concat.write_text("\n".join(lines))
    return concat


def build(
    frames_dir: Path,
    manifest_path: Path,
    background: Path,
    rendered_beats,
    out_path: Path,
    cfg,
    work_dir: Path | None = None,
) -> Path:
    """
    Assemble a finished MP4.

    frames_dir:     directory containing the PNG frames
    manifest_path:  JSON manifest from the render stage
    background:     normalized background clip (9:16, 30fps)
    rendered_beats: list[RenderedBeat] from TTS stage
    out_path:       destination MP4
    cfg:            Config object
    work_dir:       scratch directory for intermediate files (defaults to frames_dir)
    """
    work_dir = work_dir or frames_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(manifest_path.read_text())
    total_s = sum(f["ms"] for f in manifest) / 1000.0

    voice_path = _build_audio(rendered_beats, work_dir, cfg)
    concat_path = _build_concat(frames_dir, manifest)
    bg_start = _background_start_offset(background, total_s)
    logger.info("Background clip: %s, starting at %.1fs", background.name, bg_start)

    # Verify audio duration matches expected video duration
    audio_dur = _wav_duration(voice_path)
    drift = abs(audio_dur - total_s)
    if drift > 0.5:
        logger.warning(
            "Audio duration %.3fs differs from video duration %.3fs by %.3fs",
            audio_dur, total_s, drift,
        )

    # Card frames are full 1080x1920 screenshots (card position is baked in
    # via CSS), so the overlay is a plain full-frame composite.
    filt = (
        f"[0:v]scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
        f"crop={FRAME_W}:{FRAME_H},fps={cfg.video.fps}[bg];"
        f"[1:v]format=rgba[card];"
        f"[bg][card]overlay=x=0:y=0:shortest=1[v]"
    )

    if cfg.music.enabled:
        music_clips = list(Path(cfg.backgrounds.normalized_dir).parent.parent.glob("music/*.mp3"))
        music_clips += list(Path(cfg.backgrounds.normalized_dir).parent.parent.glob("music/*.wav"))
        if music_clips:
            music_path = music_clips[0]
            vol = 10 ** (cfg.music.volume_db / 20.0)
            filt += (
                f";[3:a]aloop=loop=-1:size=2e+09,atrim=end={total_s:.3f},"
                f"volume={vol:.4f}[music];"
                f"[2:a][music]amix=inputs=2:duration=first:normalize=0[a]"
            )
            cmd = _build_cmd(background, concat_path, voice_path, out_path, total_s, filt, cfg,
                              music=music_path, bg_start=bg_start)
        else:
            logger.warning("music.enabled=true but no music files found in assets/music/")
            cmd = _build_cmd(background, concat_path, voice_path, out_path, total_s, filt, cfg, bg_start=bg_start)
    else:
        cmd = _build_cmd(background, concat_path, voice_path, out_path, total_s, filt, cfg, bg_start=bg_start)

    logger.info("Running ffmpeg assembly …")
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr.decode()[-2000:]}")

    _verify_output(out_path, total_s)
    return out_path


def _background_start_offset(background: Path, total_s: float, safety_margin_s: float = 60.0) -> float:
    """Pick a random point within the background clip to start from, rather
    than always playing from its beginning — but only far enough from the end
    that the clip covers the whole video without needing to loop back to its
    own start mid-video (which would show as a jarring jump). Falls back to
    0 if the clip is too short for this video + the safety margin."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", str(background)],
            check=True, capture_output=True, text=True,
        )
        clip_duration = float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as e:
        logger.warning("Could not probe background clip duration (%s) — starting from 0", e)
        return 0.0

    max_start = clip_duration - total_s - safety_margin_s
    if max_start <= 0:
        return 0.0
    return random.uniform(0, max_start)


def _build_cmd(
    background: Path,
    concat_path: Path,
    voice_path: Path,
    out_path: Path,
    total_s: float,
    filt: str,
    cfg,
    music: Path | None = None,
    bg_start: float = 0.0,
) -> list[str]:
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{bg_start:.3f}", "-stream_loop", "-1", "-t", f"{total_s:.3f}", "-i", str(background),
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
        "-i", str(voice_path),
    ]
    if music:
        cmd += ["-stream_loop", "-1", "-t", f"{total_s:.3f}", "-i", str(music)]

    audio_map = "[a]" if music else "2:a"
    cmd += [
        "-filter_complex", filt,
        "-map", "[v]",
        "-map", audio_map,
        "-t", f"{total_s:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", str(cfg.video.fps),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    return cmd


def _wav_duration(path: Path) -> float:
    with wave.open(str(path)) as wf:
        return wf.getnframes() / wf.getframerate()


def _verify_output(out_path: Path, expected_s: float) -> None:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-show_entries", "stream=codec_type,width,height",
            "-of", "json",
            str(out_path),
        ],
        check=True, capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    actual_s = float(data["format"]["duration"])
    assert abs(actual_s - expected_s) < 0.1, (
        f"Output duration {actual_s:.3f}s differs from expected {expected_s:.3f}s"
    )

    streams = {s["codec_type"] for s in data.get("streams", [])}
    assert "video" in streams, "Output has no video stream"
    assert "audio" in streams, "Output has no audio stream"

    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert video["width"] == FRAME_W and video["height"] == FRAME_H, (
        f"Output resolution {video['width']}x{video['height']} ≠ {FRAME_W}x{FRAME_H}"
    )
    logger.info("Output verified: %.2fs, %dx%d, audio+video OK", actual_s, FRAME_W, FRAME_H)
