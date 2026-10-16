"""
Weekly long-form compilation: the week's Terraria Shorts stitched into one
16:9 video with chapters, uploaded to the same YouTube channel.

Why: Shorts views pay very little and do not count toward the Partner
Program's watch-hours route; long-form views do both. A compilation is the
cheapest long-form video this pipeline can make, since every segment already
exists and is already fact-checked.

Sources: each run's finished MP4 is attached to a GitHub release
(cfg.compilation.release) by the workflow, named "<profile>-video_<id>.mp4",
because the runner that built it is gone by Sunday. Videos whose file is not
on the release (anything built before 2026-10-16) are skipped.

Layout: the vertical video centred at full height over a blurred, zoomed copy
of itself - the usual way vertical footage is shown in a widescreen video.
"""

import json
import logging
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.captions import TERRARIA_CREDIT, _youtube_safe
from src.schema import Script

logger = logging.getLogger(__name__)

PLAYLIST = "Terraria Weekly Compilations"
TITLE_MAX = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS compilations (
  id          INTEGER PRIMARY KEY,
  week        TEXT NOT NULL UNIQUE,   -- ISO year-week, e.g. 2026-W42
  youtube_id  TEXT,                   -- NULL when skipped (too few videos)
  video_keys  TEXT,                   -- JSON list of "<profile>:<id>" included
  note        TEXT,
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _week(now) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def profile_for_db(path) -> str:
    """data/state.db -> main, data/state_<x>.db -> x."""
    stem = Path(path).stem
    return "main" if stem == "state" else stem.removeprefix("state_")


def is_due(cfg, conn, now) -> bool:
    """Compilation day, inside the posting window, and not yet done this week."""
    cc, sc = cfg.compilation, cfg.schedule
    if not cc.enabled or now.weekday() != cc.weekday:
        return False
    if not (sc.window_start_hour <= now.hour < sc.window_end_hour):
        return False
    conn.executescript(SCHEMA)
    return conn.execute("SELECT 1 FROM compilations WHERE week = ?", (_week(now),)).fetchone() is None


def _recent_videos(cfg, conn, days: int = 7) -> list[dict]:
    from src.config import profile_name
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    sql = ("SELECT id, script_json, updated_at FROM videos WHERE status = 'uploaded' "
           "AND content_format = 'terraria' AND updated_at >= ?")
    out = [dict(profile=profile_name(), id=r[0], script=r[1], at=r[2])
           for r in conn.execute(sql, (since,))]
    for path in cfg.terraria.related_dbs:
        if Path(path).exists():
            other = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                out += [dict(profile=profile_for_db(path), id=r[0], script=r[1], at=r[2])
                        for r in other.execute(sql, (since,))]
            finally:
                other.close()
    out.sort(key=lambda v: v["at"])
    return out


def _download(release: str, name: str, dest: Path) -> bool:
    r = subprocess.run(["gh", "release", "download", release, "--pattern", name,
                        "--dir", str(dest), "--clobber"], capture_output=True, text=True)
    if r.returncode != 0:
        logger.info("No original on the release for %s (%s)", name, r.stderr.strip()[:120])
    return (dest / name).exists()


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def _widescreen(src: Path, dst: Path) -> None:
    """Vertical clip -> 1920x1080 with a blurred fill. Identical encode
    settings for every segment, so the concat below can stream-copy."""
    vf = ("[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
          "boxblur=30:3,eq=brightness=-0.08[bg];"
          "[0:v]scale=-2:1080[fg];[bg][fg]overlay=(W-w)/2:0,fps=30,format=yuv420p[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", vf,
                    "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                    str(dst)], check=True)


def _timestamp(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _metadata(segments: list[tuple[Script, float]]) -> tuple[str, str, list[str]]:
    subjects = [s.title for s, _ in segments]
    title = "Terraria facts you didn't know: "
    picked = []
    for name in subjects:
        trial = ", ".join(picked + [name]) + " and more"
        if len(title + trial) > TITLE_MAX:
            break
        picked.append(name)
    title = _youtube_safe(title + ", ".join(picked) + (" and more" if len(picked) < len(subjects) else ""))

    lines, t = [], 0.0
    for script, dur in segments:
        lines.append(f"{_timestamp(t)} {_youtube_safe((script.cover_line or script.title).replace('*', ''))}")
        t += dur
    sources = sorted({s.source_url for s, _ in segments if s.source_url})
    description = "\n".join(
        [f"This week's {len(segments)} Terraria Shorts in one video.", "", *lines, "",
         TERRARIA_CREDIT, *sources, "", "#terraria #terrariatips #gaming"])[:4900]
    tags = ["terraria", "terraria tips", "terraria facts", "terraria compilation", "gaming"]
    return title, description, tags


def run(cfg, conn, now) -> str | None:
    """Build and upload this week's compilation if due. Returns the YouTube id."""
    from src.publish import youtube

    if not is_due(cfg, conn, now):
        logger.info("No compilation due.")
        return None
    cc = cfg.compilation
    week = _week(now)
    videos = _recent_videos(cfg, conn)[-cc.max_videos:]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        segments, keys, files = [], [], []
        for v in videos:
            name = f"{v['profile']}-video_{v['id']}.mp4"
            if not _download(cc.release, name, tmp):
                continue
            seg = tmp / f"seg_{len(files):03d}.mp4"
            _widescreen(tmp / name, seg)
            (tmp / name).unlink()
            segments.append((Script.model_validate_json(v["script"]), _duration(seg)))
            keys.append(f"{v['profile']}:{v['id']}")
            files.append(seg)

        if len(files) < cc.min_videos:
            note = f"only {len(files)} of {len(videos)} videos had originals (need {cc.min_videos})"
            logger.info("Skipping compilation for %s: %s", week, note)
            conn.execute("INSERT INTO compilations (week, video_keys, note) VALUES (?, ?, ?)",
                         (week, json.dumps(keys), note))
            return None

        listing = tmp / "list.txt"
        listing.write_text("".join(f"file '{f}'\n" for f in files))
        out = tmp / f"compilation_{week}.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out)],
                       check=True)

        title, description, tags = _metadata(segments)
        logger.info("Compilation %s: %d videos, %.1f min - %s", week, len(files),
                    sum(d for _, d in segments) / 60, title)
        video_id = youtube.publish_short(out, title, description, tags, cfg)

    conn.execute("INSERT INTO compilations (week, youtube_id, video_keys) VALUES (?, ?, ?)",
                 (week, video_id, json.dumps(keys)))
    try:
        youtube.add_to_playlist(cfg, video_id, PLAYLIST)
    except Exception as e:
        logger.warning("Playlist step skipped for compilation %s: %s", video_id, e)
    return video_id
