"""
Pipeline CLI. Each stage is a subcommand; run-all processes one video end to end.

Status flow:  generated → tts_done → rendered → assembled → uploaded | failed

Usage:
  python -m src.cli generate
  python -m src.cli tts         [--video-id N]
  python -m src.cli render      [--video-id N]
  python -m src.cli assemble    [--video-id N]
  python -m src.cli publish     [--video-id N]
  python -m src.cli run-all
  python -m src.cli ingest      <clip.mp4> [--crop-x N]
  python -m src.cli status
  python -m src.cli monitor
"""

import argparse
import json
import logging
import random
import shutil
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
    from src.db import insert_video
    import sqlite3

    content_format = cfg.content.format
    if content_format == "random":
        import random
        content_format = random.choice(["reddit_story", "textchain"])

    if content_format == "reddit_story":
        from src.sources.reddit import generate_reddit_script
        script = generate_reddit_script(cfg, conn)
    else:
        from src.generate.textchain import generate_script
        script = generate_script(cfg, conn)

    try:
        vid_id = insert_video(
            conn, script.premise, script.title, script.caption, script.model_dump_json(),
            content_format=content_format,
        )
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

    try:
        if row["content_format"] == "reddit_story":
            from src.render.reddit_cards import RedditCardRenderer
            renderer = RedditCardRenderer(outdir=frames_dir)
            frames = renderer.render_script(script.beats, script.card_meta, durations_ms)
        else:
            messages = [{"speaker": b.speaker, "text": b.text} for b in script.beats]
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
        cover_ms = _cover_ms_for(row["content_format"], rendered_beats)
        update_video(conn, vid_id, mp4_path=str(out_path), bg_clip=str(bg_clip), cover_ms=cover_ms, status="assembled")
        logger.info("Assembled → %s (cover_ms=%d)", out_path, cover_ms)

        if cfg.output.mobile_sync_dir:
            from src.schema import Script
            caption = Script.model_validate_json(row["script_json"]).caption
            _sync_for_mobile(cfg.output.mobile_sync_dir, vid_id, out_path, caption)
            cap_bytes = int(cfg.output.mobile_sync_cap_gb * 1024 ** 3)
            _enforce_mobile_sync_cap(cfg.output.mobile_sync_dir, cap_bytes)
    except Exception as e:
        update_video(conn, vid_id, status="failed", error=str(e))
        raise


def _sync_for_mobile(sync_dir: Path, vid_id: int, mp4_path: Path, caption: str) -> None:
    """Copy the video + a caption.txt into a synced folder (OneDrive/Dropbox/etc)
    so the caption is one tap away on the phone that actually does the TikTok
    posting — needed because TikTok's Sandbox mode doesn't reliably pre-fill
    the caption from the API (see project memory)."""
    sync_dir = Path(sync_dir)
    sync_dir.mkdir(parents=True, exist_ok=True)
    dest_mp4 = sync_dir / f"video_{vid_id}.mp4"
    dest_caption = sync_dir / f"video_{vid_id}_caption.txt"
    shutil.copy2(mp4_path, dest_mp4)
    dest_caption.write_text(caption)
    logger.info("Synced for mobile posting → %s", sync_dir)


def _enforce_mobile_sync_cap(sync_dir: Path, cap_bytes: int) -> None:
    """Delete the oldest video+caption pairs in sync_dir (by mtime) until the
    folder's total size is back under cap_bytes. Only touches this OneDrive
    courtesy copy — the publish stage uploads from output.ready_dir, which is
    untouched by this cleanup, so deleting here never breaks a pending post."""
    sync_dir = Path(sync_dir)
    mp4s = sorted(sync_dir.glob("video_*.mp4"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in sync_dir.glob("*") if p.is_file())

    i = 0
    while total > cap_bytes and i < len(mp4s):
        mp4_path = mp4s[i]
        caption_path = mp4_path.with_name(mp4_path.stem + "_caption.txt")
        freed = mp4_path.stat().st_size
        mp4_path.unlink()
        if caption_path.exists():
            freed += caption_path.stat().st_size
            caption_path.unlink()
        total -= freed
        logger.info("Mobile sync cap exceeded — deleted %s (%.1f MB freed)", mp4_path.name, freed / 1024**2)
        i += 1


def _cover_ms_for(content_format: str, rendered_beats) -> int:
    """Pick a millisecond offset into the video for the platform-facing cover/
    thumbnail frame — clicking through in-feed depends on this, and a mid-
    animation frame (e.g. a chat bubble half-popped-in) looks broken as a still.
    Clamped to land safely within the first beat, before the second one starts."""
    first_beat_ms = rendered_beats[0].duration_ms + rendered_beats[0].gap_ms
    if content_format == "reddit_story":
        target = 500.0   # static full-block card for the whole beat — any safe point works
    else:
        from src.render.cards import ENTRY_MS
        target = ENTRY_MS + 200.0   # past the bubble pop-in animation, fully "settled"
    return int(min(target, max(first_beat_ms - 100.0, 0.0)))


# ── publish ───────────────────────────────────────────────────────────────────

def _publish_row(conn, cfg, row, platform: str) -> bool:
    """Publish one video row to the given platform(s). Returns whether at
    least one platform succeeded. Shared by cmd_publish and cmd_auto_publish."""
    from src.db import update_video
    from src.schema import Script
    from src.captions import build_caption
    from src.publish.instagram import publish_reel, CredentialsMissing as IGMissing
    from src.publish.tiktok import upload_draft, CredentialsMissing as TTMissing

    vid_id = row["id"]

    already_done = all(
        row[f"{p}_id"] for p in ("instagram", "tiktok") if platform in ("both", p)
    )
    if already_done:
        logger.info("Video %d already published to all requested platform(s) for this call.", vid_id)
        return True

    script = Script.model_validate_json(row["script_json"])
    mp4_path = Path(row["mp4_path"])
    cover_ms = row["cover_ms"] if row["cover_ms"] is not None else 500
    published_any = False

    # Rebuilt fresh here rather than trusting script.caption — a video
    # generated before a caption-convention change would otherwise carry a
    # stale caption forever, since nothing else re-derives it later.
    caption = build_caption(row["content_format"], script.hook, script.tags)

    if platform in ("both", "instagram") and row["instagram_id"]:
        logger.info("Video %d already has an Instagram post (%s) — not re-posting.",
                    vid_id, row["instagram_id"])
    elif platform in ("both", "instagram"):
        try:
            ig_id = publish_reel(mp4_path, caption, cfg, conn=conn, cover_ms=cover_ms)
            update_video(conn, vid_id, instagram_id=ig_id)
            logger.info("Published to Instagram: %s", ig_id)
            published_any = True
        except IGMissing as e:
            logger.warning("Instagram skipped: %s", e)
        except Exception as e:
            # Any real failure (network, malformed upload, API-side rejection)
            # must not crash the whole publish call — that would leave the row
            # stuck mid-status and skip finalizing the other platform / the
            # schedule slot below. Log loudly and move on; the row stays
            # eligible for a retry since instagram_id is never set on failure.
            logger.error("Instagram publish failed for video %d: %s", vid_id, e)

    if platform in ("both", "tiktok") and row["tiktok_id"]:
        logger.info("Video %d already has a TikTok upload (%s) — not re-uploading.",
                    vid_id, row["tiktok_id"])
    elif platform in ("both", "tiktok"):
        try:
            tt_id = upload_draft(mp4_path, caption, cfg, conn=conn, cover_ms=cover_ms)
            update_video(conn, vid_id, tiktok_id=tt_id)
            logger.info("TikTok draft submitted: %s", tt_id)
            published_any = True
        except TTMissing as e:
            logger.warning("TikTok skipped: %s", e)
        except Exception as e:
            logger.error("TikTok publish failed for video %d: %s", vid_id, e)

    if published_any:
        # Safe to mark 'uploaded' even for a single-platform call: the
        # already_done/per-platform *_id checks above (not this status field)
        # are what gate re-publishing, so a later publish to an untouched
        # platform is never blocked by this. Previously this only fired when
        # platform=="both", which left every row stuck at 'uploading' forever
        # once schedule.platform was set to a single platform (e.g.
        # "instagram" while TikTok's review is pending) — fixed 2026-09-04.
        update_video(conn, vid_id, status="uploaded")
    else:
        logger.warning("%s publish did not succeed for video %d — stays as 'assembled'", platform, vid_id)
        update_video(conn, vid_id, status="assembled")

    return published_any


def cmd_publish(args):
    cfg = _cfg()
    conn = _db(cfg)
    from src.db import claim_next

    if args.video_id:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (args.video_id,)).fetchone()
        if row is None:
            logger.error("Video %d not found.", args.video_id)
            sys.exit(1)
        if row["status"] == "uploaded":
            logger.info("Video %d is already uploaded (tiktok_id=%s, instagram_id=%s) — skipping.",
                        row["id"], row["tiktok_id"], row["instagram_id"])
            return
    else:
        row = claim_next(conn, "assembled", "uploading")

    if not row:
        logger.info("No videos in 'assembled' state.")
        return

    platform = getattr(args, "platform", "both")
    _publish_row(conn, cfg, row, platform)


# ── auto-publish (randomized daily schedule) ────────────────────────────────

def _ensure_and_get_due_slots(conn, cfg, now):
    """
    Shared by cmd_auto_publish (always-on host: backlog + posting are separate
    ticks) and cmd_cloud_tick (ephemeral host: one tick does everything).
    On first call each day, randomizes cfg.schedule.posts_per_day target times
    within [window_start_hour, window_end_hour) and stores them. Returns
    (all_of_todays_slots, due_unfired_slots) for the caller to act on.
    """
    import random
    from datetime import timedelta
    from src.db import get_todays_slots, create_slots, due_unfired_slots

    sc = cfg.schedule
    today = now.strftime("%Y-%m-%d")

    slots = get_todays_slots(conn, today)
    if not slots:
        window_start = now.replace(hour=sc.window_start_hour, minute=0, second=0, microsecond=0)
        window_end = now.replace(hour=sc.window_end_hour, minute=0, second=0, microsecond=0)
        window_seconds = (window_end - window_start).total_seconds()
        if window_seconds <= 0:
            logger.error("schedule.window_end_hour must be after window_start_hour")
            return [], []

        # Reject-and-resample so no two slots land closer than min_gap_minutes —
        # simplest correct approach for a handful of slots per day.
        min_gap_s = sc.min_gap_minutes * 60
        chosen: list[float] = []
        attempts = 0
        while len(chosen) < sc.posts_per_day and attempts < 500:
            attempts += 1
            candidate = random.uniform(0, window_seconds)
            if all(abs(candidate - c) >= min_gap_s for c in chosen):
                chosen.append(candidate)
        chosen.sort()

        slot_times = [(window_start + timedelta(seconds=s)).isoformat() for s in chosen]
        create_slots(conn, today, slot_times)
        logger.info("Randomized %d post times for %s: %s", len(slot_times), today,
                    [t[11:16] for t in slot_times])
        slots = get_todays_slots(conn, today)

    due = due_unfired_slots(conn, today, now.isoformat())
    return slots, due


def cmd_auto_publish(args):
    """
    Meant to be invoked frequently (e.g. every 10-15 min via Task Scheduler)
    on an always-on host. Publishes the oldest ready 'assembled' video for any
    slot whose time has passed and hasn't fired yet. If no video is ready when
    a slot comes due, the slot stays pending and is retried on the next call
    rather than skipped — a temporarily-empty queue shouldn't cost a post slot
    for the day.

    Deliberately separate from run-all: generation/tts/render/assemble should
    happen continuously to keep a buffer of ready videos, but actual posting
    should only happen at the randomized times. Run both as separate
    Task Scheduler jobs. (Not used on the GitHub Actions/ephemeral-runner path
    — see cmd_cloud_tick, which can't rely on a backlog surviving between runs.)
    """
    from datetime import datetime
    from src.db import claim_next, mark_slot_fired

    cfg = _cfg()
    conn = _db(cfg)
    sc = cfg.schedule
    now = datetime.now()

    slots, due = _ensure_and_get_due_slots(conn, cfg, now)
    if not due:
        upcoming = [s["slot_time"][11:16] for s in slots if not s["fired"]]
        logger.info("No slots due yet. Upcoming today: %s", upcoming or "none left")
        return

    for slot in due:
        row = claim_next(conn, "assembled", "uploading")
        if not row:
            logger.warning(
                "Slot %s is due but no assembled video is ready — will retry next check "
                "instead of skipping the slot.", slot["slot_time"][11:16]
            )
            break   # earlier slots take priority; don't skip ahead to a later one either
        published = _publish_row(conn, cfg, row, sc.platform)
        mark_slot_fired(conn, slot["id"], row["id"])
        logger.info("Slot %s: published video %d (%s)", slot["slot_time"][11:16], row["id"],
                    "ok" if published else "no platforms configured")


def cmd_cloud_tick(args):
    """
    Entry point for an ephemeral scheduled runner (GitHub Actions) instead of
    an always-on host. Unlike run-all/auto-publish's design (build a backlog
    continuously on one tick, post from it on another), nothing here can rely
    on intermediate work surviving between invocations except data/state.db
    itself (which the workflow commits back to the repo) — there's no
    always-on disk to hold a backlog of assembled-but-unpublished videos.

    So: only when a schedule slot is actually due does this tick do any real
    work at all, and when it does, it runs generate → tts → render → assemble
    → publish for ONE video synchronously in a single process, so nothing
    partially-finished needs to persist afterward. Most invocations (no slot
    due yet) exit almost immediately and cost near-zero Actions minutes.
    """
    from datetime import datetime
    from src.db import get_video, mark_slot_fired

    cfg = _cfg()
    conn = _db(cfg)
    sc = cfg.schedule
    now = datetime.now()

    slots, due = _ensure_and_get_due_slots(conn, cfg, now)
    if not due:
        upcoming = [s["slot_time"][11:16] for s in slots if not s["fired"]]
        logger.info("No slots due yet. Upcoming today: %s", upcoming or "none left")
        return

    slot = due[0]   # one video per invocation is enough — any other due slots
                     # are picked up by the next scheduled tick, same as a
                     # temporarily-empty queue is handled on the VM path.
    logger.info("Slot %s is due — generating a video now (no backlog on this runner).",
                slot["slot_time"][11:16])

    vid_id = cmd_generate(args)
    stage_args = argparse.Namespace(video_id=vid_id)
    cmd_tts(stage_args)
    cmd_render(stage_args)
    cmd_assemble(stage_args)

    row = get_video(conn, vid_id)
    if row["status"] != "assembled":
        logger.error(
            "Video %d did not reach 'assembled' (status=%s) — slot %s stays unfired, "
            "will retry next tick.", vid_id, row["status"], slot["slot_time"][11:16]
        )
        return

    published = _publish_row(conn, cfg, row, sc.platform)
    mark_slot_fired(conn, slot["id"], vid_id)
    logger.info("Slot %s: published video %d (%s)", slot["slot_time"][11:16], vid_id,
                "ok" if published else "no platforms configured")


# ── run-all ───────────────────────────────────────────────────────────────────

# Maps each status to the function that advances it to the next. 'assembled'
# is deliberately NOT here — publishing is owned exclusively by the
# auto-publish scheduler (cmd_auto_publish), which posts at randomized daily
# times. run-all's job is only to keep building backlog; it must never touch
# an assembled video, or every video would get published the instant it
# finishes rendering instead of at its scheduled slot (this actually happened
# live on 2026-09-03 before this fix — run-all was racing auto-publish and
# always won, since it runs the moment a video reaches 'assembled').
_STAGE_FN = {
    "generated":  cmd_tts,
    "tts_done":   cmd_render,
    "rendered":   cmd_assemble,
}


def cmd_run_all(args):
    """Generate if queue is empty, then advance the oldest unfinished video by one stage.
    Stops at 'assembled' — that status is claimed only by auto-publish, never by run-all."""
    cfg = _cfg()
    conn = _db(cfg)

    pending = conn.execute(
        "SELECT id, status FROM videos "
        "WHERE status NOT IN ('uploaded', 'failed', 'archived', 'assembled', "
        "'tts_running', 'rendering', 'assembling', 'uploading') "
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

    stage_args = argparse.Namespace(video_id=vid_id)
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
    else:
        print(f"{'Status':<15} {'Count':>5}")
        print("-" * 22)
        for r in rows:
            print(f"{r['status']:<15} {r['n']:>5}")
        total = sum(r["n"] for r in rows)
        print("-" * 22)
        print(f"{'TOTAL':<15} {total:>5}")

    print()
    print("Platform tokens:")
    from src.publish.tiktok import token_status as tt_status
    from src.publish.instagram import token_status as ig_status
    for name, fn in (("tiktok", tt_status), ("instagram", ig_status)):
        info = fn(cfg, conn)
        if not info["configured"]:
            print(f"  {name:<10} not configured")
        elif info["expires_at"] is None:
            if name == "instagram":
                print(f"  {name:<10} configured — non-expiring Page token, no auto-refresh (re-auth manually if ever rejected)")
            else:
                print(f"  {name:<10} configured (env), no refresh recorded yet — will refresh on first publish")
        else:
            print(f"  {name:<10} expires at {info['expires_at']}")


# ── monitor ───────────────────────────────────────────────────────────────────

def cmd_monitor(args):
    """
    Lightweight account-health check for both platforms — run this periodically
    (e.g. via the same Task Scheduler mechanism as run-all) to catch problems
    automatically instead of noticing a reach drop-off by accident.

    Checks: token still valid (a broken/revoked token is the #1 way these
    pipelines die silently), and follower/post-count trend. Does NOT check
    engagement/reach — that needs instagram_manage_insights (not yet granted)
    and, for Instagram Reels specifically, is gated behind 1,000 followers by
    the platform regardless of permissions. See project memory for the full
    reasoning; this will need extending once those are cleared.
    """
    from src.db import record_account_metrics, latest_account_metrics
    from src.publish.instagram import check_account_health as ig_health
    from src.publish.tiktok import check_account_health as tt_health

    cfg = _cfg()
    conn = _db(cfg)

    for name, fn in (("instagram", ig_health), ("tiktok", tt_health)):
        result = fn(cfg, conn)
        if result.get("error") == "not configured":
            print(f"{name}: not configured, skipping")
            continue

        record_account_metrics(
            conn, name,
            token_ok=result["token_ok"],
            followers_count=result.get("followers_count"),
            media_count=result.get("media_count"),
            error=result.get("error"),
        )

        if not result["token_ok"]:
            logger.warning("%s: TOKEN CHECK FAILED — %s", name, result.get("error"))
            print(f"{name}: ⚠ token check FAILED — {result.get('error')}")
            continue

        history = latest_account_metrics(conn, name, limit=2)
        followers = result.get("followers_count")
        media = result.get("media_count")
        line = f"{name}: ok — followers={followers}, posts={media}"
        if len(history) == 2 and history[1]["followers_count"] is not None and followers is not None:
            delta = followers - history[1]["followers_count"]
            if delta != 0:
                line += f" ({'+' if delta > 0 else ''}{delta} since last check)"
        print(line)


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
    p_publish.add_argument("--platform", choices=["both", "instagram", "tiktok"], default="both",
                            help="Restrict to one platform (e.g. for manual testing) instead of publishing to both")

    sub.add_parser("run-all", help="Advance or generate one video end to end")
    sub.add_parser("auto-publish", help="Publish at randomized daily times (cfg.schedule) — separate from run-all")
    sub.add_parser("cloud-tick", help="Ephemeral-runner entry point (GitHub Actions): generate+publish one video only when a slot is due")

    p_ingest = sub.add_parser("ingest", help="Normalize a background clip")
    p_ingest.add_argument("clip", help="Path to source clip")
    p_ingest.add_argument("--crop-x", type=int, default=None)

    sub.add_parser("status", help="Show video counts by status")
    sub.add_parser("monitor", help="Check token health + follower/post trend for both platforms")

    args = parser.parse_args()
    dispatch = {
        "generate":  cmd_generate,
        "tts":       cmd_tts,
        "render":    cmd_render,
        "assemble":  cmd_assemble,
        "publish":   cmd_publish,
        "run-all":   cmd_run_all,
        "auto-publish": cmd_auto_publish,
        "cloud-tick": cmd_cloud_tick,
        "ingest":    cmd_ingest,
        "status":    cmd_status,
        "monitor":   cmd_monitor,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
