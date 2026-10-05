"""
Daily pipeline. Run once a day shortly after 00:00 UTC.

  1. Record the new day's holdings          backfill_history.py --days 2
  2. Extend the transfer scan to now        scan_transfers.py
  3. Refresh holder lists (weekly, Mondays) fetch_holders.py
  4. Rebuild point-in-time cohorts          build_cohorts.py
  5. Export the static site                 export_site.py
  6. Prune and compact the DB (Sundays)     prune_db.py

Stops at the first failing step, so a broken step never publishes partial data;
the previous export stays live. A lock file prevents overlapping runs.

Usage:
    python scripts/run_daily.py
    python scripts/run_daily.py --refresh-holders   # force step 3
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
PY = sys.executable
LOCK = ROOT / "data" / ".daily.lock"
_STALE_LOCK_SECONDS = 6 * 3600


def step(name: str, args: list[str]) -> None:
    started = time.time()
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {name}…", flush=True)
    result = subprocess.run([PY, "-u", *args], cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"FAILED at '{name}' (exit {result.returncode}); site not updated")
    print(f"    done in {time.time() - started:.0f}s", flush=True)


def acquire_lock() -> None:
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < _STALE_LOCK_SECONDS:
        raise SystemExit(f"Another run holds {LOCK} — exiting")
    LOCK.write_text(str(os.getpid()))


def main():
    parser = argparse.ArgumentParser(description="Run the daily data pipeline")
    parser.add_argument("--refresh-holders", action="store_true")
    args = parser.parse_args()

    acquire_lock()
    try:
        step("Record today's holdings", ["scripts/backfill_history.py", "--days", "2"])
        step("Extend transfer scan", ["scripts/scan_transfers.py"])
        if args.refresh_holders or datetime.now(timezone.utc).weekday() == 0:
            step("Refresh holder lists", ["scripts/fetch_holders.py"])
        step("Rebuild point-in-time cohorts", ["scripts/build_cohorts.py"])
        step("Export site", ["scripts/export_site.py"])
        if datetime.now(timezone.utc).weekday() == 6:
            step("Prune and compact database", ["scripts/prune_db.py"])
        print("Daily pipeline complete.")
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
