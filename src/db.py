"""
SQLite state store. One row per video, status advances through:
  generated -> rendered -> assembled -> uploaded | failed

Dedup is enforced by a UNIQUE constraint on premise_hash.
claim_next() atomically advances a row so a crashed run resumes cleanly.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id            INTEGER PRIMARY KEY,
  premise       TEXT NOT NULL,
  premise_hash  TEXT NOT NULL UNIQUE,
  title         TEXT,
  caption       TEXT,
  script_json   TEXT NOT NULL,
  status        TEXT NOT NULL,   -- generated|tts_done|rendered|assembled|uploaded|failed
  content_format TEXT NOT NULL DEFAULT 'textchain',  -- textchain | reddit_story
  beats_json    TEXT,            -- JSON list[RenderedBeat]; set after TTS stage
  frames_dir    TEXT,            -- path to PNG frames directory; set after render stage
  manifest_path TEXT,            -- path to manifest.json; set after render stage
  bg_clip       TEXT,
  mp4_path      TEXT,
  cover_ms      INTEGER,        -- ms offset for the platform-facing cover/thumbnail frame
  tiktok_id     TEXT,
  instagram_id  TEXT,
  error         TEXT,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_status ON videos(status);

CREATE TABLE IF NOT EXISTS tokens (
  platform      TEXT PRIMARY KEY,   -- 'tiktok' | 'instagram'
  access_token  TEXT NOT NULL,
  refresh_token TEXT,
  expires_at    TIMESTAMP NOT NULL,
  updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS account_metrics (
  id              INTEGER PRIMARY KEY,
  platform        TEXT NOT NULL,        -- 'tiktok' | 'instagram'
  fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  followers_count INTEGER,
  media_count     INTEGER,
  token_ok        INTEGER NOT NULL,     -- 0/1 — did the auth check itself succeed
  error           TEXT
);

CREATE TABLE IF NOT EXISTS schedule_slots (
  id          INTEGER PRIMARY KEY,
  date        TEXT NOT NULL,       -- YYYY-MM-DD, local time
  slot_time   TIMESTAMP NOT NULL,  -- the randomized target time for this slot
  fired       INTEGER NOT NULL DEFAULT 0,
  video_id    INTEGER,
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_schedule_date ON schedule_slots(date);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema, for DBs created before them."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    if "content_format" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN content_format TEXT NOT NULL DEFAULT 'textchain'")
    if "cover_ms" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN cover_ms INTEGER")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = _connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def premise_hash(premise: str) -> str:
    return hashlib.sha256(premise.strip().lower().encode()).hexdigest()[:16]


def insert_video(
    conn: sqlite3.Connection,
    premise: str,
    title: str,
    caption: str,
    script_json: str,
    bg_clip: Optional[str] = None,
    content_format: str = "textchain",
) -> int:
    """Insert a new video row. Raises sqlite3.IntegrityError on duplicate premise."""
    cur = conn.execute(
        """
        INSERT INTO videos (premise, premise_hash, title, caption, script_json, status, bg_clip, content_format)
        VALUES (?, ?, ?, ?, ?, 'generated', ?, ?)
        """,
        (premise, premise_hash(premise), title, caption, script_json, bg_clip, content_format),
    )
    return cur.lastrowid


def claim_next(
    conn: sqlite3.Connection,
    from_status: str,
    to_status: str,
) -> Optional[sqlite3.Row]:
    """
    Atomically select the oldest row in from_status and advance it to to_status.
    Returns the row (pre-advance values + new status) or None if nothing to claim.
    """
    with conn:
        row = conn.execute(
            "SELECT id FROM videos WHERE status = ? ORDER BY created_at LIMIT 1",
            (from_status,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE videos SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (to_status, row["id"]),
        )
        return conn.execute("SELECT * FROM videos WHERE id = ?", (row["id"],)).fetchone()


def update_video(conn: sqlite3.Connection, video_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = "CURRENT_TIMESTAMP"
    set_clause = ", ".join(
        f"{k} = CURRENT_TIMESTAMP" if v == "CURRENT_TIMESTAMP" else f"{k} = ?"
        for k, v in fields.items()
    )
    values = [v for v in fields.values() if v != "CURRENT_TIMESTAMP"]
    conn.execute(f"UPDATE videos SET {set_clause} WHERE id = ?", (*values, video_id))


def get_video(conn: sqlite3.Connection, video_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()


def premise_exists(conn: sqlite3.Connection, premise: str) -> bool:
    h = premise_hash(premise)
    row = conn.execute(
        "SELECT 1 FROM videos WHERE premise_hash = ?", (h,)
    ).fetchone()
    return row is not None


def get_token(conn: sqlite3.Connection, platform: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM tokens WHERE platform = ?", (platform,)).fetchone()


def set_token(
    conn: sqlite3.Connection,
    platform: str,
    access_token: str,
    expires_at: str,
    refresh_token: Optional[str] = None,
) -> None:
    """Upsert a platform's token. refresh_token is left unchanged if not provided
    (TikTok's refresh response includes a new one each time; Instagram's does not)."""
    conn.execute(
        """
        INSERT INTO tokens (platform, access_token, refresh_token, expires_at, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(platform) DO UPDATE SET
          access_token = excluded.access_token,
          refresh_token = COALESCE(excluded.refresh_token, tokens.refresh_token),
          expires_at = excluded.expires_at,
          updated_at = CURRENT_TIMESTAMP
        """,
        (platform, access_token, refresh_token, expires_at),
    )


def record_account_metrics(
    conn: sqlite3.Connection,
    platform: str,
    token_ok: bool,
    followers_count: Optional[int] = None,
    media_count: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO account_metrics (platform, followers_count, media_count, token_ok, error)
        VALUES (?, ?, ?, ?, ?)
        """,
        (platform, followers_count, media_count, int(token_ok), error),
    )


def latest_account_metrics(conn: sqlite3.Connection, platform: str, limit: int = 2) -> list[sqlite3.Row]:
    """Most recent checks first — used to compare against the prior reading."""
    return conn.execute(
        "SELECT * FROM account_metrics WHERE platform = ? ORDER BY fetched_at DESC LIMIT ?",
        (platform, limit),
    ).fetchall()


def get_todays_slots(conn: sqlite3.Connection, date: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM schedule_slots WHERE date = ? ORDER BY slot_time", (date,)
    ).fetchall()


def create_slots(conn: sqlite3.Connection, date: str, slot_times: list[str]) -> None:
    """slot_times: ISO timestamps. Only called when no slots exist yet for this date."""
    with conn:
        for st in slot_times:
            conn.execute(
                "INSERT INTO schedule_slots (date, slot_time) VALUES (?, ?)", (date, st)
            )


def due_unfired_slots(conn: sqlite3.Connection, date: str, now_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM schedule_slots WHERE date = ? AND fired = 0 AND slot_time <= ? ORDER BY slot_time",
        (date, now_iso),
    ).fetchall()


def mark_slot_fired(conn: sqlite3.Connection, slot_id: int, video_id: Optional[int]) -> None:
    conn.execute(
        "UPDATE schedule_slots SET fired = 1, video_id = ? WHERE id = ?", (video_id, slot_id)
    )
