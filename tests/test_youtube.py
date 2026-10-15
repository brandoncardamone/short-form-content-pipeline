"""
Offline tests for YouTube Shorts publishing: metadata, the resumable upload,
platform parsing and the multi-platform publish loop. No network.
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from src import cli, db
from src.captions import TERRARIA_CREDIT, build_youtube_metadata
from src.config import Config, platforms_for
from src.publish import instagram, youtube
from src.schema import Beat, Script


def _script(**kw):
    base = dict(beats=[Beat(speaker="a", text="hi", voice="v")], title="Secret world seeds",
                hook="Type THIS as your world name", tags=["seeds", "worldgen"],
                caption="", premise="p", cover_line="Secret *world* seeds",
                source_url="https://terraria.wiki.gg/wiki/Secret_world_seeds")
    base.update(kw)
    return Script(**base)


# ── metadata ──────────────────────────────────────────────────────────────────

def test_title_uses_the_title_card_line_without_emphasis_marks():
    title, _, _ = build_youtube_metadata("terraria", _script())
    assert title == "Secret world seeds #terraria #shorts"


def test_long_titles_are_cut_on_a_word_and_stay_under_100():
    title, _, _ = build_youtube_metadata("terraria", _script(cover_line="word " * 40))
    assert len(title) <= 100 and title.endswith("word #terraria #shorts")


def test_angle_brackets_are_removed_everywhere():
    title, desc, _ = build_youtube_metadata("terraria", _script(cover_line="a <b> c",
                                                                hook="x > y"))
    assert "<" not in title + desc and ">" not in title + desc


def test_description_keeps_the_wiki_credit_and_source():
    _, desc, tags = build_youtube_metadata("terraria", _script())
    assert TERRARIA_CREDIT in desc
    assert "terraria.wiki.gg/wiki/Secret_world_seeds" in desc
    assert "terraria" in tags and "shorts" in tags
    assert sum(len(t) + 1 for t in tags) <= 500


# ── platforms ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("both", ["instagram", "tiktok"]),
    ("instagram", ["instagram"]),
    ("instagram,youtube", ["instagram", "youtube"]),
    (" youtube , instagram ,youtube", ["youtube", "instagram"]),
])
def test_platforms_for(value, expected):
    assert platforms_for(value) == expected


def test_unknown_platform_is_an_error_not_a_silent_skip():
    with pytest.raises(ValueError):
        platforms_for("instagram,youtub")


# ── upload ────────────────────────────────────────────────────────────────────

def _resp(status, body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = (__import__("json").dumps(body) if body is not None else "").encode()
    return r


def _cfg(**kw):
    c = Config(youtube_refresh_token="r", youtube_client_id="i", youtube_client_secret="s")
    return c.model_copy(update=kw)


@pytest.fixture
def mp4(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"0123456789" * 10)
    return f


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)


def test_upload_resumes_from_the_committed_offset(monkeypatch, mp4):
    puts = []

    def post(url, **kw):
        if url == youtube.TOKEN_URL:
            return _resp(200, {"access_token": "tok"})
        assert kw["json"]["status"]["containsSyntheticMedia"] is True
        return _resp(200, {}, {"Location": "https://upload/session"})

    def put(url, headers=None, data=None, timeout=None):
        puts.append((dict(headers or {}), len(data or b"")))
        if len(puts) == 1:
            raise requests.ConnectionError("dropped")
        if headers.get("Content-Range", "").startswith("bytes */"):
            return _resp(308, headers={"Range": "bytes=0-39"})
        return _resp(200, {"id": "abc", "status": {"uploadStatus": "uploaded",
                                                   "privacyStatus": "public"}})

    monkeypatch.setattr(youtube.requests, "post", post)
    monkeypatch.setattr(youtube.requests, "put", put)
    assert youtube.publish_short(mp4, "t", "d", ["x"], _cfg()) == "abc"
    resumed_headers, resumed_len = puts[-1]
    assert resumed_headers["Content-Range"] == "bytes 40-99/100"
    assert resumed_len == 60


def test_a_rejected_upload_raises(monkeypatch, mp4):
    monkeypatch.setattr(youtube.requests, "post", lambda url, **kw: (
        _resp(200, {"access_token": "t"}) if url == youtube.TOKEN_URL
        else _resp(200, {}, {"Location": "s"})))
    monkeypatch.setattr(youtube.requests, "put", lambda *a, **kw: _resp(
        200, {"id": "x", "status": {"uploadStatus": "rejected", "rejectionReason": "duplicate"}}))
    with pytest.raises(RuntimeError, match="duplicate"):
        youtube.publish_short(mp4, "t", "d", [], _cfg())


def test_a_revoked_refresh_token_is_token_expired(monkeypatch):
    monkeypatch.setattr(youtube.requests, "post",
                        lambda *a, **kw: _resp(400, {"error": "invalid_grant"}))
    with pytest.raises(youtube.TokenExpired):
        youtube.access_token(_cfg())


def test_missing_credentials_skip_cleanly():
    with pytest.raises(youtube.CredentialsMissing):
        youtube.access_token(Config())


# ── publish loop ──────────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path, mp4):
    c = db.init_db(tmp_path / "state.db")
    c.execute("""INSERT INTO videos (id, premise, premise_hash, script_json, status,
                 content_format, mp4_path, cover_ms) VALUES (1, 'p', 'h', ?, 'uploading',
                 'terraria', ?, 600)""", (_script().model_dump_json(), str(mp4)))
    return c


def _row(conn):
    return conn.execute("SELECT * FROM videos WHERE id = 1").fetchone()


def test_youtube_failure_after_instagram_success_is_recorded(monkeypatch, conn):
    monkeypatch.setattr(instagram, "publish_reel", lambda *a, **kw: "ig1")

    def boom(*a, **kw):
        raise RuntimeError("quota")
    monkeypatch.setattr(youtube, "publish_short", boom)

    assert cli._publish_row(conn, Config(), _row(conn), "instagram,youtube")
    r = _row(conn)
    assert (r["status"], r["instagram_id"], r["youtube_id"]) == ("uploaded", "ig1", None)
    assert r["error"].startswith("partial: youtube: quota")


def test_both_succeed_and_nothing_is_reposted(monkeypatch, conn):
    calls = []
    monkeypatch.setattr(instagram, "publish_reel", lambda *a, **kw: calls.append("ig") or "ig1")
    monkeypatch.setattr(youtube, "publish_short", lambda *a, **kw: calls.append("yt") or "yt1")

    assert cli._publish_row(conn, Config(), _row(conn), "instagram,youtube")
    assert cli._publish_row(conn, Config(), _row(conn), "instagram,youtube")
    assert calls == ["ig", "yt"]
    r = _row(conn)
    assert (r["instagram_id"], r["youtube_id"], r["error"]) == ("ig1", "yt1", None)


def test_youtube_without_credentials_does_not_block_instagram(monkeypatch, conn):
    monkeypatch.setattr(instagram, "publish_reel", lambda *a, **kw: "ig1")
    assert cli._publish_row(conn, Config(), _row(conn), "instagram,youtube")
    assert _row(conn)["error"] is None


def test_gate_counts_youtube_as_a_destination():
    cfg = _cfg()
    cfg.schedule.platform = "youtube"
    assert cli._publish_credentials_missing(cfg) is None
    cfg.schedule.platform = "instagram"
    assert cli._publish_credentials_missing(cfg) is not None


def test_old_databases_gain_youtube_id(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE videos (id INTEGER PRIMARY KEY, premise TEXT NOT NULL,
                   premise_hash TEXT NOT NULL UNIQUE, script_json TEXT NOT NULL,
                   status TEXT NOT NULL, instagram_id TEXT, tiktok_id TEXT)""")
    old.commit()
    old.close()
    cols = {r["name"] for r in db.init_db(path).execute("PRAGMA table_info(videos)")}
    assert "youtube_id" in cols
