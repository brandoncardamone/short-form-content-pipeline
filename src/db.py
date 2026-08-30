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
  beats_json    TEXT,            -- JSON list[RenderedBeat]; set after TTS stage
  frames_dir    TEXT,            -- path to PNG frames directory; set after render stage
  manifest_path TEXT,            -- path to manifest.json; set after render stage
  bg_clip       TEXT,
  mp4_path      TEXT,
  tiktok_id     TEXT,
  instagram_id  TEXT,
  error         TEXT,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_status ON videos(status);
"""


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
) -> int:
    """Insert a new video row. Raises sqlite3.IntegrityError on duplicate premise."""
    cur = conn.execute(
        """
        INSERT INTO videos (premise, premise_hash, title, caption, script_json, status, bg_clip)
        VALUES (?, ?, ?, ?, ?, 'generated', ?)
        """,
        (premise, premise_hash(premise), title, caption, script_json, bg_clip),
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
