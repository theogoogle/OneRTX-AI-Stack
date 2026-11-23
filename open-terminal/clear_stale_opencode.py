"""Clear stale suspended OpenCode sessions so interrupted tasks do not auto-replay across UIs.

Safe under `python -I` (stdlib only). Checks the local router `/health` endpoint first and
skips cleanup whenever any LLM request is currently in flight (unless `--force` is passed).
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
HEALTH_URL = "http://127.0.0.1:4000/health"


def _router_busy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return int((data.get("gpu") or {}).get("llm_inflight") or 0) > 0
    except Exception:
        return False


def clear_stale(db_path: Path = DB_PATH, min_age_s: int = 180, force: bool = False) -> int:
    if not db_path.is_file():
        print("0 stale OpenCode sessions")
        return 0
    if not force and _router_busy():
        print("skipped: router busy")
        return 0

    cutoff_ms = int(time.time() * 1000) - max(0, int(min_age_s)) * 1000
    con = sqlite3.connect(str(db_path), timeout=2.0)
    try:
        cur = con.execute(
            """
            UPDATE session_v2
            SET time_suspended = NULL,
                time_idle = COALESCE(time_idle, time_updated, time_created),
                idle_outcome = COALESCE(idle_outcome, 'failed'),
                resume_attempts = 0
            WHERE (time_suspended IS NOT NULL OR (time_busy IS NOT NULL AND time_idle IS NULL))
              AND COALESCE(time_updated, time_created, 0) < ?
            """,
            (cutoff_ms,),
        )
        cleared = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        con.commit()
    finally:
        con.close()

    if cleared:
        print(f"cleared {cleared} stale OpenCode session(s)")
    else:
        print("0 stale OpenCode sessions")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clear stale suspended OpenCode sessions.")
    parser.add_argument("--min-age-s", type=int, default=180, help="Minimum idle age in seconds (default: 180).")
    parser.add_argument("--force", action="store_true", help="Run even if router reports llm_inflight > 0.")
    args = parser.parse_args(argv)
    return clear_stale(min_age_s=args.min_age_s, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
