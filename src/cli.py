"""
Pipeline CLI. Each stage is a subcommand; run-all processes one video end to end.

Status flow:  generated → tts_done → rendered → assembled → uploaded | failed

Usage:
  python -m src.cli generate
  python -m src.cli tts         [--video-id N]
  python -m src.cli render      [--video-id N]
  python -m src.cli assemble    [--video-id N]
  python -m src.cli publish     [--video-id N] [--video-url URL]
  python -m src.cli run-all     [--video-url URL]
  python -m src.cli ingest      <clip.mp4> [--crop-x N]
  python -m src.cli status
"""

import argparse
import json
import logging
import random
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cli")


def _cfg():
    from src.config import load_config
    return load_config()


def _db(cfg):
    from src.db import init_db
    return init_db(Path(cfg.output.db_path))


# ── generate ──────────────────────────────────────────────────────────────────

def cmd_generate(args):
    cfg = _cfg()
    conn = _db(cfg)
    from src.generate.textchain import generate_script
    from src.db import insert_video
    import sqlite3

    script = generate_script(cfg, conn)
    try:
        vid_id = insert_video(conn, script.premise, script.title, script.caption, script.model_dump_json())
    except sqlite3.IntegrityError:
        logger.error("Duplicate premise_hash — this should have been caught by generate_script")
        sys.exit(1)

    logger.info("Generated video id=%d title=%r", vid_id, script.title)
    return vid_id


# ── tts ───────────────────────────────────────────────────────────────────────

def cmd_tts(args):
    cfg = _cfg()
    conn = _db(cfg)
    from src.db import claim_next, update_video
    from src.schema import Script
    from src.tts.run import run_tts

    if args.video_id:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (args.video_id,)).fetchone()
        if row is None:
            logger.error("Video %d not found.", args.video_id)
            sys.exit(1)
    else:
        row = claim_next(conn, "generated", "tts_running")

    if not row:
        logger.info("No videos in 'generated' state.")
        return

    vid_id = row["id"]
    script = Script.model_validate_json(row["script_json"])
    work_dir = Path(cfg.output.work_dir) / f"video_{vid_id}"

    try:
        rendered_beats = run_tts(script, work_dir / "tts", cfg)
        beats_json = json.dumps([rb.model_dump(mode="json") for rb in rendered_beats], default=str)
        update_video(conn, vid_id, beats_json=beats_json, status="tts_done")
        logger.info("TTS done for video %d — %d beats", vid_id, len(rendered_beats))
    except Exception as e:
        update_video(conn, vid_id, status="failed", error=str(e))
        raise


# ── render ────────────────────────────────────────────────────────────────────

def cmd_render(args):
    cfg = _cfg()
    conn = _db(cfg)
    from src.db import claim_next, update_video
    from src.schema import Script, RenderedBeat
    from src.render.cards import CardRenderer

    if args.video_id:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (args.video_id,)).fetchone()
        if row is None:
            logger.error("Video %d not found.", args.video_id)
            sys.exit(1)
    else:
        row = claim_next(conn, "tts_done", "rendering")

    if not row:
        logger.info("No videos in 'tts_done' state.")
        return

    vid_id = row["id"]
    script = Script.model_validate_json(row["script_json"])
    beats_data = json.loads(row["beats_json"])
    rendered_beats = [RenderedBeat.model_validate(b) for b in beats_data]

    work_dir = Path(cfg.output.work_dir) / f"video_{vid_id}"
    frames_dir = work_dir / "frames"

    durations_ms = [rb.duration_ms + rb.gap_ms for rb in rendered_beats]
    messages = [{"speaker": b.speaker, "text": b.text} for b in script.beats]

    try:
        renderer = CardRenderer(
            contact_name=cfg.card.contact_name,
            avatar=cfg.card.avatar,
            outdir=frames_dir,
        )
        frames = renderer.render_script(messages, durations_ms)

        manifest_path = frames_dir / "manifest.json"
        manifest_path.write_text(json.dumps(
            [{"path": f.path.name, "ms": f.duration_ms} for f in frames], indent=1
        ))

        update_video(
            conn, vid_id,
            frames_dir=str(frames_dir),
            manifest_path=str(manifest_path),
            status="rendered",
        )
        logger.info("Rendered %d frames for video %d", len(frames), vid_id)
    except Exception as e:
        update_video(conn, vid_id, status="failed", error=str(e))
        raise


# ── assemble ──────────────────────────────────────────────────────────────────

def cmd_assemble(args):
    cfg = _cfg()
    conn = _db(cfg)
    from src.db import claim_next, update_video
    from src.schema import RenderedBeat
    from src.assemble.build import build

    if args.video_id:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (args.video_id,)).fetchone()
        if row is None:
            logger.error("Video %d not found.", args.video_id)
            sys.exit(1)
    else:
        row = claim_next(conn, "rendered", "assembling")

    if not row:
        logger.info("No videos in 'rendered' state.")
        return

    vid_id = row["id"]
    rendered_beats = [RenderedBeat.model_validate(b) for b in json.loads(row["beats_json"])]
    frames_dir = Path(row["frames_dir"])
    manifest_path = Path(row["manifest_path"])
    work_dir = Path(cfg.output.work_dir) / f"video_{vid_id}"

    normalized_dir = Path(cfg.backgrounds.normalized_dir)
    bg_clips = list(normalized_dir.glob("*.mp4"))
    if not bg_clips:
        logger.error("No normalized background clips in %s. Run `ingest` first.", normalized_dir)
        sys.exit(1)

    bg_clip = bg_clips[0] if len(bg_clips) == 1 else random.choice(bg_clips)
    out_path = Path(cfg.output.ready_dir) / f"video_{vid_id}.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        build(frames_dir, manifest_path, bg_clip, rendered_beats, out_path, cfg, work_dir=work_dir)
        update_video(conn, vid_id, mp4_path=str(out_path), bg_clip=str(bg_clip), status="assembled")
        logger.info("Assembled → %s", out_path)
    except Exception as e:
        update_video(conn, vid_id, status="failed", error=str(e))
        raise


# ── publish ───────────────────────────────────────────────────────────────────

def cmd_publish(args):
    cfg = _cfg()
    conn = _db(cfg)
    from src.db import claim_next, update_video
    from src.schema import Script
    from src.publish.instagram import publish_reel, CredentialsMissing as IGMissing
    from src.publish.tiktok import upload_draft, CredentialsMissing as TTMissing

    if args.video_id:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (args.video_id,)).fetchone()
        if row is None:
            logger.error("Video %d not found.", args.video_id)
            sys.exit(1)
    else:
        row = claim_next(conn, "assembled", "uploading")

    if not row:
        logger.info("No videos in 'assembled' state.")
        return

    vid_id = row["id"]
    script = Script.model_validate_json(row["script_json"])
    mp4_path = Path(row["mp4_path"])
    published_any = False

    video_url = getattr(args, "video_url", None)
    if video_url:
        try:
            ig_id = publish_reel(video_url, script.caption, cfg)
            update_video(conn, vid_id, instagram_id=ig_id)
            logger.info("Published to Instagram: %s", ig_id)
            published_any = True
        except IGMissing as e:
            logger.warning("Instagram skipped: %s", e)
    else:
        logger.info("Instagram skipped: --video-url not provided")

    try:
        tt_id = upload_draft(mp4_path, script.caption, cfg)
        update_video(conn, vid_id, tiktok_id=tt_id)
        logger.info("TikTok draft submitted: %s", tt_id)
        published_any = True
    except TTMissing as e:
        logger.warning("TikTok skipped: %s", e)

    if published_any:
        update_video(conn, vid_id, status="uploaded")
    else:
        logger.warning("No platform credentials configured — video %d stays as 'assembled'", vid_id)
        update_video(conn, vid_id, status="assembled")


# ── run-all ───────────────────────────────────────────────────────────────────

# Maps each status to the function that advances it to the next
_STAGE_FN = {
    "generated":  cmd_tts,
    "tts_done":   cmd_render,
    "rendered":   cmd_assemble,
    "assembled":  cmd_publish,
}


def cmd_run_all(args):
    """Generate if queue is empty, then advance the oldest unfinished video by one stage."""
    cfg = _cfg()
    conn = _db(cfg)

    pending = conn.execute(
        "SELECT id, status FROM videos "
        "WHERE status NOT IN ('uploaded', 'failed', 'tts_running', 'rendering', 'assembling', 'uploading') "
        "ORDER BY created_at LIMIT 1"
    ).fetchone()

    if not pending:
        logger.info("Queue empty — generating new script …")
        cmd_generate(args)
        pending = conn.execute(
            "SELECT id, status FROM videos WHERE status = 'generated' ORDER BY created_at LIMIT 1"
        ).fetchone()

    if not pending:
        logger.info("Nothing to process after generate.")
        return

    vid_id, status = pending["id"], pending["status"]
    stage_fn = _STAGE_FN.get(status)
    if stage_fn is None:
        logger.warning("Video %d is in unhandled status %r", vid_id, status)
        return

    stage_args = argparse.Namespace(video_id=vid_id, video_url=getattr(args, "video_url", None))
    logger.info("Advancing video %d from status=%s", vid_id, status)
    stage_fn(stage_args)


# ── ingest ────────────────────────────────────────────────────────────────────

def cmd_ingest(args):
    from src.assemble.ingest import ingest_clip
    logging.getLogger().setLevel(logging.DEBUG)
    cfg = _cfg()
    clip = Path(args.clip)
    out = ingest_clip(clip, Path(cfg.backgrounds.normalized_dir), crop_x=args.crop_x)
    print(f"Normalized → {out}")


# ── status ────────────────────────────────────────────────────────────────────

def cmd_status(args):
    cfg = _cfg()
    conn = _db(cfg)
    rows = conn.execute(
        "SELECT status, count(*) as n FROM videos GROUP BY status ORDER BY status"
    ).fetchall()
    if not rows:
        print("No videos in database.")
        return
    print(f"{'Status':<15} {'Count':>5}")
    print("-" * 22)
    for r in rows:
        print(f"{r['status']:<15} {r['n']:>5}")
    total = sum(r["n"] for r in rows)
    print("-" * 22)
    print(f"{'TOTAL':<15} {total:>5}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Short-form content pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate", help="Generate a new script via LLM")

    p_tts = sub.add_parser("tts", help="Run TTS on a generated script")
    p_tts.add_argument("--video-id", type=int, default=None)

    p_render = sub.add_parser("render", help="Render card frames")
    p_render.add_argument("--video-id", type=int, default=None)

    p_assemble = sub.add_parser("assemble", help="Assemble final MP4")
    p_assemble.add_argument("--video-id", type=int, default=None)

    p_publish = sub.add_parser("publish", help="Upload to platforms")
    p_publish.add_argument("--video-id", type=int, default=None)
    p_publish.add_argument("--video-url", default=None,
                           help="Public URL of the MP4 (required for Instagram Graph API)")

    p_all = sub.add_parser("run-all", help="Advance or generate one video end to end")
    p_all.add_argument("--video-url", default=None)

    p_ingest = sub.add_parser("ingest", help="Normalize a background clip")
    p_ingest.add_argument("clip", help="Path to source clip")
    p_ingest.add_argument("--crop-x", type=int, default=None)

    sub.add_parser("status", help="Show video counts by status")

    args = parser.parse_args()
    dispatch = {
        "generate":  cmd_generate,
        "tts":       cmd_tts,
        "render":    cmd_render,
        "assemble":  cmd_assemble,
        "publish":   cmd_publish,
        "run-all":   cmd_run_all,
        "ingest":    cmd_ingest,
        "status":    cmd_status,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
