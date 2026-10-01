"""
Records daily on-chain holdings (wallet + staked positions) for every tracked
address — whales and insiders — at the first block of each UTC day.

Backfill a year:      python scripts/backfill_history.py --days 365
Daily snapshot (cron): python scripts/backfill_history.py --days 1

Days already recorded for an address are skipped, so re-runs are cheap.

Note: this is the history of *today's* cohort. Wallets that were whales a year ago
but sold out aren't in it, so it shows what current whales did — it is not a
point-in-time whale index and mustn't be backtested as one.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TOKEN_BASKET
from db.schema import init_db, get_connection
from chain.rpc import batch, block_at_timestamp, latest_block
from chain.positions import POSITION_SOURCES, holdings, position_participants


def _block_ts(n: int) -> int:
    return int(batch([("eth_getBlockByNumber", [hex(n), False])])[0]["timestamp"], 16)


_SLOTS_PER_DAY = 7200   # 12-second slots; missed slots make the real count slightly lower


# Blocks per day vary with missed slots (7,070–7,180 over the past year; one
# network-incident day had 6,765), so search rather than assume.
_BLOCKS_PER_DAY_EST = 7150


def _first_block_at_or_after(ts: int, prev: tuple[int, int] | None, per_day: int) -> int:
    """
    Interpolation search: block timestamps grow almost linearly, so guessing by
    interpolation lands within a few blocks — ~4 single-block reads per day instead
    of ~20 for bisection. `prev` is (block, timestamp) of a known earlier block.
    """
    if not prev:
        return block_at_timestamp(ts - 1) + 1     # last block before ts, plus one
    lo, lo_ts = prev                              # lo_ts < ts
    head = latest_block()                         # callers only pass ts <= head's timestamp
    hi = min(head, lo + round((ts - lo_ts) / 86400 * per_day) + 300)
    hi_ts = _block_ts(hi)
    while hi_ts < ts:                             # rare: estimate fell short
        lo, lo_ts = hi, hi_ts
        hi = min(head, hi + 2000)
        hi_ts = _block_ts(hi)
    # Invariant: ts(lo) < ts <= ts(hi); answer is the smallest such hi
    while hi - lo > 1:
        guess = lo + max(1, min(hi - lo - 1, round((ts - lo_ts) / (hi_ts - lo_ts) * (hi - lo))))
        g_ts = _block_ts(guess)
        if g_ts < ts:
            lo, lo_ts = guess, g_ts
        else:
            hi, hi_ts = guess, g_ts
    return hi


def day_block(date: str, prev: tuple[int, int] | None = None, per_day: int = _BLOCKS_PER_DAY_EST) -> int:
    """First block at or after 00:00 UTC on `date`, cached in daily_blocks."""
    con = get_connection()
    row = con.execute("SELECT block_number FROM daily_blocks WHERE date = ?", (date,)).fetchone()
    if row:
        con.close()
        return row[0]
    midnight = int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    block = _first_block_at_or_after(midnight, prev, per_day)
    con.execute("INSERT OR REPLACE INTO daily_blocks (date, block_number, block_timestamp) VALUES (?,?,?)",
                (date, block, _block_ts(block)))
    con.commit()
    con.close()
    return block


def _cached_ts(date: str) -> int:
    con = get_connection()
    ts = con.execute("SELECT block_timestamp FROM daily_blocks WHERE date = ?", (date,)).fetchone()[0]
    con.close()
    return ts


def tracked_addresses(symbol: str) -> list[str]:
    con = get_connection()
    rows = con.execute("SELECT address FROM holders WHERE token_symbol = ?", (symbol,)).fetchall()
    con.close()
    return [r[0] for r in rows]


def missing(symbol: str, date: str, addresses: list[str]) -> list[str]:
    con = get_connection()
    done = {r[0] for r in con.execute(
        "SELECT address FROM holdings_daily WHERE token_symbol = ? AND date = ?", (symbol, date))}
    con.close()
    return [a for a in addresses if a not in done]


def record_day(symbol: str, token: dict, date: str, block: int, addresses: list[str]) -> int:
    todo = missing(symbol, date, addresses)
    if not todo:
        return 0
    positions = position_participants(symbol, block) if symbol in POSITION_SOURCES else {}
    held = holdings(symbol, token, todo, block, positions)
    con = get_connection()
    con.executemany(
        """INSERT OR REPLACE INTO holdings_daily
           (token_symbol, address, date, block_number, wallet_balance, staked_balance, total)
           VALUES (?,?,?,?,?,?,?)""",
        [(symbol, a, date, block, h["wallet"], sum(h["positions"].values()), h["total"])
         for a, h in held.items()],
    )
    con.commit()
    con.close()
    return len(todo)


def main():
    parser = argparse.ArgumentParser(description="Record daily cohort holdings")
    parser.add_argument("--days", type=int, default=1, help="How many UTC days back from today")
    parser.add_argument("--symbol", default=None)
    args = parser.parse_args()

    init_db()
    basket = {args.symbol: TOKEN_BASKET[args.symbol]} if args.symbol else TOKEN_BASKET
    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=d)).isoformat() for d in range(args.days - 1, -1, -1)]
    head = latest_block()

    head_ts = _block_ts(head)
    dates = [d for d in dates
             if datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() <= head_ts]
    print(f"Resolving {len(dates)} day boundary blocks...")
    blocks, prev, per_day = {}, None, _BLOCKS_PER_DAY_EST
    for d in dates:
        blocks[d] = day_block(d, prev, per_day)
        if prev and 6000 < blocks[d] - prev[0] < 7300:
            per_day = blocks[d] - prev[0]
        prev = (blocks[d], _cached_ts(d))

    for symbol, token in basket.items():
        addrs = tracked_addresses(symbol)
        if not addrs:
            print(f"{symbol}: no tracked holders — run fetch_holders.py first")
            continue
        written = 0
        for d in dates:
            written += record_day(symbol, token, d, blocks[d], addrs)
        print(f"{symbol:<6} {len(addrs):>3} addresses × {len(dates)} days — {written:,} new rows")


if __name__ == "__main__":
    main()
