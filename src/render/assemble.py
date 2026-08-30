"""
Assembles card frames over a background clip.

Strategy: write an ffmpeg concat demuxer manifest holding each PNG for its
measured duration, decode that as one input, and overlay it on the background
in a single pass. Avoids per-frame Python compositing entirely.
"""

import json
import subprocess
from pathlib import Path

FRAME_W, FRAME_H = 1080, 1920
CARD_CENTER_Y = 0.48


def build(frames_dir, manifest_path, background, out_path, fps=30):
    frames_dir = Path(frames_dir)
    manifest = json.loads(Path(manifest_path).read_text())
    total_s = sum(f["ms"] for f in manifest) / 1000.0

    # concat demuxer list: each PNG with an explicit duration
    concat = frames_dir / "concat.txt"
    lines = []
    for f in manifest:
        lines.append(f"file '{frames_dir.resolve()}/{f['path']}'")
        lines.append(f"duration {f['ms']/1000:.4f}")
    lines.append(f"file '{frames_dir.resolve()}/{manifest[-1]['path']}'")
    concat.write_text("\n".join(lines))

    # Card is centered horizontally; vertical anchor is the card's midpoint,
    # so overlay y depends on each frame's height -> use overlay expression.
    filt = (
        f"[0:v]scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
        f"crop={FRAME_W}:{FRAME_H},fps={fps}[bg];"
        f"[1:v]format=rgba[card];"
        f"[bg][card]overlay=x=(W-w)/2:y={CARD_CENTER_Y}*H-h/2:shortest=1[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-t", f"{total_s:.3f}", "-i", str(background),
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-filter_complex", filt,
        "-map", "[v]",
        "-t", f"{total_s:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


if __name__ == "__main__":
    here = Path(__file__).parent.parent
    build(here / "out", here / "out" / "manifest.json",
          here / "out" / "bg.mp4", here / "out" / "demo.mp4")
    print("built demo.mp4")
