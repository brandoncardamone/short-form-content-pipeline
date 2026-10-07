"""Delete every cached token from the state databases, then prove none remain.

Run by the workflow immediately before it commits a state database back to
this PUBLIC repo. The `tokens` table caches refreshed access/refresh tokens,
and one reaching a commit would be a real credential leak. `_current_token()`
falls back to the env-supplied secret when the DB has none, so scrubbing costs
nothing functionally.

Scrubs the paths given as arguments, or every data/state*.db when given none.
Each account profile has its own database (data/state.db,
data/state_terraria.db, ...) and each one is committed, so the default has to
be "all of them": a hardcoded data/state.db would leave every other profile's
tokens in place while still reporting success.

This lives in a file rather than inline in the workflow YAML on purpose: as an
inline `python -c` string it depended on `python` existing on PATH, and when it
did not the scrub failed while the commit still went ahead. Exits non-zero if
anything is left, so the commit step cannot proceed on a dirty database.
"""

import sqlite3
import sys
from pathlib import Path


def scrub(db: Path) -> int:
    if not db.exists():
        print(f"{db} does not exist - nothing to scrub.")
        return 0

    conn = sqlite3.connect(db)
    try:
        try:
            conn.execute("DELETE FROM tokens")
            conn.commit()
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            print(f"{db}: no tokens table - nothing to scrub.")
            return 0

        left = conn.execute("SELECT count(*) FROM tokens").fetchone()[0]
    finally:
        conn.close()

    if left:
        print(f"REFUSING TO COMMIT: {left} row(s) still in the tokens table of {db}",
              file=sys.stderr)
        return 1

    print(f"{db}: tokens table clean")
    return 0


def main() -> int:
    dbs = [Path(a) for a in sys.argv[1:]] or sorted(Path("data").glob("state*.db"))
    if not dbs:
        print("No state databases - nothing to scrub.")
        return 0
    # Scrub every one before reporting, rather than stopping at the first failure.
    return max(scrub(db) for db in dbs)


if __name__ == "__main__":
    sys.exit(main())
