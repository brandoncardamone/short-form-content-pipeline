"""Decide, cheaply, whether this run has any work to do.

Most scheduled runs do not. Before this existed they found that out only after
restoring ~4.6GB of caches and installing torch, chatterbox and Chromium -
about four minutes of runner time to print "outside the posting window". On
2026-09-29 two of four runs were exactly that.

Writes `proceed=true|false` to $GITHUB_OUTPUT; the workflow gates every
expensive step on it.

It reuses cmd_cloud_tick's own helpers rather than reimplementing the checks,
which is the only reason this is safe - every heavy import in src.cli is
function-local, so importing it costs nothing here. cloud-tick still makes the
real decision when it runs; this only skips work that is certainly pointless.
On any error it fails OPEN (proceed=true), because a wrongly-skipped run costs
a post while a wrongly-started one costs four minutes.
"""

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def decide() -> tuple[bool, str]:
    # Imported lazily so an import failure is caught and fails open.
    sys.argv = ["should_run"]
    from src.cli import (
        _cfg,
        _db,
        _minutes_since_last_publish,
        _now_in_schedule_tz,
        _posts_today,
        _publish_credentials_missing,
    )

    cfg = _cfg()
    sc = cfg.schedule
    conn = _db(cfg)
    now = _now_in_schedule_tz(cfg)

    missing = _publish_credentials_missing(cfg)
    if missing:
        return False, missing

    if not (sc.window_start_hour <= now.hour < sc.window_end_hour):
        return False, (f"{now:%H:%M} is outside the posting window "
                       f"{sc.window_start_hour:02d}:00-{sc.window_end_hour:02d}:00 {sc.timezone}")

    done = _posts_today(conn, cfg, now)
    if done >= sc.posts_per_day:
        return False, f"already posted {done}/{sc.posts_per_day} today"

    gap = _minutes_since_last_publish(conn)
    if gap is not None and gap < sc.min_gap_minutes:
        return False, (f"last post was {gap:.0f} min ago, "
                       f"min_gap_minutes is {sc.min_gap_minutes}")

    return True, f"post {done + 1}/{sc.posts_per_day} for today is due"


def main() -> int:
    try:
        proceed, reason = decide()
    except Exception:
        traceback.print_exc()
        proceed, reason = True, "gate failed - proceeding so a real run is never skipped"

    print(f"proceed={str(proceed).lower()} - {reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"proceed={str(proceed).lower()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
