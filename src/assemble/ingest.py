"""
Background clip ingest. Normalizes source clips to 1080x1920 / 30fps / no audio
and writes them to assets/backgrounds/normalized/.

Runs once per clip; already-normalized clips are skipped. Per-video rendering
consumes only the pre-normalized files, so no scaling happens at render time.

Aspect ratio handling:
  - 9:16 (w/h ≈ 0.5625):   scale to 1080x1920
  - 16:9 or wider:          scale to height 1920, then crop width to 1080.
                            Crop X offset comes from per-clip config so off-center
                            action can be corrected; defaults to center crop.
  - Other:                  scale-to-fill then center crop (logged as warning).
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

TARGET_W, TARGET_H = 1080, 1920
TARGET_FPS = 30
ASPECT_TOLERANCE = 0.05   # how close w/h must be to 0.5625 to count as "already 9:16"
ASPECT_9_16 = 9 / 16


def probe(clip_path: Path) -> dict:
    """Return width, height, fps (float), duration (float) via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,duration",
            "-of", "json",
            str(clip_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)["streams"][0]
    num, den = data["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    return {
        "width": int(data["width"]),
        "height": int(data["height"]),
        "fps": fps,
        "duration": float(data.get("duration", 0)),
    }


def _scale_and_crop_filter(width: int, height: int, crop_x: int | None) -> str:
    aspect = width / height
    delta = abs(aspect - ASPECT_9_16)

    if delta <= ASPECT_TOLERANCE:
        # Already 9:16 — just scale
        return f"scale={TARGET_W}:{TARGET_H}:flags=lanczos,fps={TARGET_FPS}"

    if aspect > ASPECT_9_16:
        # Wider than 9:16 — scale to target height, then crop width
        scaled_w = int(round(TARGET_H * aspect / 2) * 2)   # keep even
        x = crop_x if crop_x is not None else (scaled_w - TARGET_W) // 2
        if crop_x is None:
            logger.debug("Using center crop x=%d for %dx%d clip", x, width, height)
        else:
            logger.debug("Using configured crop x=%d for %dx%d clip", x, width, height)
        return (
            f"scale={scaled_w}:{TARGET_H}:flags=lanczos,"
            f"crop={TARGET_W}:{TARGET_H}:{x}:0,"
            f"fps={TARGET_FPS}"
        )

    # Taller than 9:16 — scale to target width, then crop height
    logger.warning(
        "Clip %dx%d has unusual aspect ratio %.3f. Scaling to fill and center-cropping.",
        width, height, aspect,
    )
    scaled_h = int(round(TARGET_W / aspect / 2) * 2)
    y = (scaled_h - TARGET_H) // 2
    return (
        f"scale={TARGET_W}:{scaled_h}:flags=lanczos,"
        f"crop={TARGET_W}:{TARGET_H}:0:{y},"
        f"fps={TARGET_FPS}"
    )


def ingest_clip(
    clip_path: Path,
    normalized_dir: Path,
    crop_x: int | None = None,
    force: bool = False,
) -> Path:
    """
    Normalize one clip. Returns path to the normalized file.
    Skips if already present and force=False.
    """
    normalized_dir.mkdir(parents=True, exist_ok=True)
    out_path = normalized_dir / clip_path.name

    if out_path.exists() and not force:
        logger.info("Already normalized: %s", out_path.name)
        return out_path

    info = probe(clip_path)
    vf = _scale_and_crop_filter(info["width"], info["height"], crop_x)
    logger.info(
        "Ingesting %s (%dx%d, %.2ffps) → %s",
        clip_path.name, info["width"], info["height"], info["fps"], out_path.name,
    )

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(clip_path),
            "-vf", vf,
            "-an",                    # strip audio — background clips are silent
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",             # higher quality than final render; source for later
            "-pix_fmt", "yuv420p",
            str(out_path),
        ],
        check=True,
        capture_output=True,
    )
    return out_path


def ingest_all(cfg) -> list[Path]:
    """Ingest all clips listed in config. Returns list of normalized paths."""
    from src.config import BackgroundClipConfig

    normalized_dir = Path(cfg.backgrounds.normalized_dir)
    results = []

    if not cfg.backgrounds.clips:
        logger.warning("No background clips configured. Add entries under backgrounds.clips in config.yaml.")
        return results

    for clip_cfg in cfg.backgrounds.clips:
        clip_path = Path(clip_cfg.path)
        if not clip_path.exists():
            logger.error("Clip not found: %s", clip_path)
            continue
        out = ingest_clip(clip_path, normalized_dir, crop_x=clip_cfg.crop_x)
        results.append(out)

    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python -m src.assemble.ingest <clip.mp4> [--crop-x N]")
        sys.exit(1)

    clip = Path(sys.argv[1])
    cx = None
    if "--crop-x" in sys.argv:
        cx = int(sys.argv[sys.argv.index("--crop-x") + 1])

    from src.config import load_config
    cfg = load_config()
    out = ingest_clip(clip, Path(cfg.backgrounds.normalized_dir), crop_x=cx)
    print(f"Normalized → {out}")
