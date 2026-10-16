"""
Offline tests for the YouTube growth pieces: the YouTube-only profile, cross-
stream dedup, learning angle weights from analytics, playlists, the shared-
channel posting gap and the weekly compilation. No network, no LLM.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from src import cli, compile_weekly, config, db
from src.config import Config
from src.publish import youtube
from src.schema import Beat, Script
from src.sources import terraria as T


def _script(angle=None, hook=None, title="Pylons", **kw):
    return Script(beats=[Beat(text="hi", speaker="a", voice="v")], title=title, hook="h",
                  tags=[], caption="", premise=f"terraria:{title}", angle=angle,
                  hook_style=hook, cover_line=f"{title} *secret*",
                  source_url=f"https://terraria.wiki.gg/wiki/{title}", **kw)


def _add(conn, vid, script, yt=None, days_ago=5, status="uploaded"):
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""INSERT INTO videos (id, premise, premise_hash, script_json, status,
                    content_format, youtube_id, updated_at) VALUES (?, ?, ?, ?, ?, 'terraria', ?, ?)""",
                 (vid, script.premise, db.premise_hash(script.premise), script.model_dump_json(),
                  status, yt, at))


@pytest.fixture
def two_dbs(tmp_path):
    a = db.init_db(tmp_path / "state_terraria.db")
    b = db.init_db(tmp_path / "state_terraria_yt.db")
    cfg = Config()
    cfg.terraria.related_dbs = [tmp_path / "state_terraria.db"]
    return a, b, cfg


# ── profile ───────────────────────────────────────────────────────────────────

def test_youtube_only_profile_borrows_the_channel_login_and_never_instagram(monkeypatch):
    monkeypatch.setattr(config, "_config", None)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **kw: None)
    for k in list(os.environ):
        if k.startswith(("YOUTUBE_", "INSTAGRAM_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("PROFILE", "terraria_yt")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN_TERRARIA", "channel-token")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "main-account")
    cfg = config.load_config()
    monkeypatch.setattr(config, "_config", None)
    assert cfg.youtube_refresh_token == "channel-token"
    assert cfg.instagram_access_token is None
    assert config.platforms_for(cfg.schedule.platform) == ["youtube"]
    assert cfg.video.target_duration_max <= 45
    assert cfg.output.db_path == Path("data/state_terraria_yt.db")


# ── dedup across the two streams ──────────────────────────────────────────────

def test_a_page_used_by_the_other_stream_counts_as_used(two_dbs):
    a, b, cfg = two_dbs
    _add(a, 1, _script(title="Shimmer"))
    assert T._used(b, cfg, "terraria:Shimmer")
    assert not T._used(b, cfg, "terraria:Pylons")


# ── learning from analytics ───────────────────────────────────────────────────

def test_angles_whose_videos_get_more_views_are_weighted_up(monkeypatch, two_dbs):
    a, b, cfg = two_dbs
    cfg.terraria.learn_from_analytics = True
    good, bad = T.ANGLES[0][0], T.ANGLES[1][0]
    views = {}
    for i in range(6):
        angle = good if i % 2 == 0 else bad
        _add(a, i + 1, _script(angle=angle, hook=T.HOOKS[0][0], title=f"P{i}"), yt=f"y{i}")
        views[f"y{i}"] = {"views": 5000 if angle == good else 50}
    _add(a, 99, _script(angle=bad, title="Fresh"), yt="fresh", days_ago=0)   # too young to count
    views["fresh"] = {"views": 10 ** 7}
    monkeypatch.setattr(youtube, "fetch_analytics", lambda cfg: views)

    angles, hooks = T._learned_weights(cfg, b)
    w = dict(angles)
    assert w[good] > T.ANGLES[0][1] and w[bad] < T.ANGLES[1][1]
    assert w[good] <= T.ANGLES[0][1] * T.LEARN_CLAMP[1]
    untouched = T.ANGLES[5]
    assert w[untouched[0]] == pytest.approx(untouched[1])


def test_learning_fails_open_without_analytics(monkeypatch, two_dbs):
    _, b, cfg = two_dbs
    cfg.terraria.learn_from_analytics = True

    def refuse(cfg):
        raise RuntimeError("403 insufficient scope")
    monkeypatch.setattr(youtube, "fetch_analytics", refuse)
    assert T._learned_weights(cfg, b) == (T.ANGLES, T.HOOKS)


def test_learning_is_off_unless_configured(two_dbs):
    _, b, cfg = two_dbs
    assert T._learned_weights(cfg, b) == (T.ANGLES, T.HOOKS)


# ── playlists ─────────────────────────────────────────────────────────────────

def test_every_angle_has_a_playlist():
    names = {T.playlist_for(a) for a, _ in T.ANGLES}
    assert None not in names and "Terraria Myths Busted" in names
    assert T.playlist_for(None) is None


# ── shared-channel gap ────────────────────────────────────────────────────────

def test_youtube_only_stream_waits_for_the_shared_post(two_dbs):
    a, b, cfg = two_dbs
    _add(a, 1, _script(), days_ago=0)       # shared post just now
    assert cli._minutes_since_last_publish(b, cfg) is None
    cfg.schedule.gap_includes_related = True
    assert cli._minutes_since_last_publish(b, cfg) < 5


# ── weekly compilation ────────────────────────────────────────────────────────

def test_compilation_is_due_once_per_week_on_its_day(two_dbs):
    _, b, cfg = two_dbs
    cfg.compilation.enabled = True
    sunday = datetime(2026, 10, 18, 15, 0)
    assert compile_weekly.is_due(cfg, b, sunday)
    assert not compile_weekly.is_due(cfg, b, sunday - timedelta(days=1))
    assert not compile_weekly.is_due(cfg, b, sunday.replace(hour=6))
    b.execute("INSERT INTO compilations (week, note) VALUES (?, 'skipped')",
              (compile_weekly._week(sunday),))
    assert not compile_weekly.is_due(cfg, b, sunday)


def test_compilation_metadata_has_chapters_and_credit():
    segs = [(_script(title=f"Subject number {i}"), 42.0) for i in range(12)]
    title, desc, _ = compile_weekly._metadata(segs)
    assert len(title) <= 100 and title.endswith("and more")
    lines = desc.splitlines()
    assert "0:00 Subject number 0 secret" in lines and "7:42 Subject number 11 secret" in lines
    assert "Terraria Wiki" in desc


def test_recent_videos_come_from_both_streams_with_their_profile(two_dbs, monkeypatch):
    a, b, cfg = two_dbs
    monkeypatch.setattr(config, "profile_name", lambda: "terraria_yt")
    _add(a, 1, _script(title="A"), days_ago=2)
    _add(b, 1, _script(title="B"), days_ago=1)
    _add(b, 2, _script(title="Old"), days_ago=20)
    got = [(v["profile"], v["id"]) for v in compile_weekly._recent_videos(cfg, b)]
    assert got == [("terraria", 1), ("terraria_yt", 1)]
    assert compile_weekly.profile_for_db("data/state.db") == "main"
