"""Delete every cached token from data/state.db, then prove none remain.

Run by the workflow immediately before it commits data/state.db back to this
PUBLIC repo. The `tokens` table caches refreshed access/refresh tokens, and one
reaching a commit would be a real credential leak. `_current_token()` falls
back to the env-supplied secret when the DB has none, so scrubbing costs
nothing functionally.

This lives in a file rather than inline in the workflow YAML on purpose: as an
inline `python -c` string it depended on `python` existing on PATH, and when it
did not the scrub failed while the commit still went ahead. Exits non-zero if
anything is left, so the commit step cannot proceed on a dirty database.
"""

import sqlite3
import sys
from pathlib import Path

DB = Path("data/state.db")


def main() -> int:
    if not DB.exists():
        print(f"{DB} does not exist - nothing to scrub.")
        return 0

    conn = sqlite3.connect(DB)
    try:
        conn.execute("DELETE FROM tokens")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            raise
        print("No tokens table - nothing to scrub.")
        return 0

    left = conn.execute("SELECT count(*) FROM tokens").fetchone()[0]
    if left:
        print(
            f"REFUSING TO COMMIT: {left} row(s) still in the tokens table",
            file=sys.stderr,
        )
        return 1

    print("tokens table clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
