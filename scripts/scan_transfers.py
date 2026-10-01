"""
Scans a year of transfers per token into transfer_totals (resumable).

Usage:
    python scripts/scan_transfers.py
    python scripts/scan_transfers.py --symbol LINK --days 366
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TOKEN_BASKET
from db.schema import init_db, get_connection
from chain.rpc import latest_block
from chain.transfers import scan


def main():
    parser = argparse.ArgumentParser(description="Aggregate a period of token transfers per address")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--days", type=int, default=366)
    args = parser.parse_args()

    init_db()
    start_date = (datetime.now(timezone.utc).date() - timedelta(days=args.days - 1)).isoformat()
    con = get_connection()
    row = con.execute("SELECT block_number FROM daily_blocks WHERE date = ?", (start_date,)).fetchone()
    con.close()
    if not row:
        sys.exit(f"No day block for {start_date} — run backfill_history.py --days {args.days} first")
    from_block, to_block = row[0], latest_block()

    basket = {args.symbol: TOKEN_BASKET[args.symbol]} if args.symbol else TOKEN_BASKET
    for symbol, token in basket.items():
        print(f"{symbol}: scanning blocks {from_block:,}–{to_block:,} (from {start_date})")
        n = scan(symbol, token["contract"], token["decimals"], from_block, to_block)
        print(f"{symbol}: done, {n:,} transfers this run")


if __name__ == "__main__":
    main()
