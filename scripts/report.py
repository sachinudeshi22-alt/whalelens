"""
Whale vs insider flow summary from holdings_daily.

For each token and window (default 30/90/365 days), compares holdings on the
latest recorded date against the date `window` days earlier:
  Δ%       — change in the group's combined holdings (wallet + staked)
  breadth  — how many members grew vs shrank by more than 1%
  % supply — the group's combined holdings as a share of total supply now

Breadth matters because one large holder can swing the combined number.

Usage:
    python scripts/report.py
    python scripts/report.py --windows 7,30
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TOKEN_BASKET
from db.schema import get_connection
from chain.rpc import multicall

TOTAL_SUPPLY = "0x18160ddd"
_MOVE = 0.01   # members moving less than this count as flat


def total_supplies() -> dict[str, float]:
    syms = list(TOKEN_BASKET)
    res = multicall([(TOKEN_BASKET[s]["contract"], TOTAL_SUPPLY) for s in syms])
    return {s: int.from_bytes(r[:32], "big") / 10 ** TOKEN_BASKET[s]["decimals"] for s, r in zip(syms, res)}


def group_change(con, symbol: str, category: str, start: str, end: str) -> dict | None:
    rows = con.execute(
        """SELECT h.address, a.total, b.total
           FROM holders h
           JOIN holdings_daily a ON a.token_symbol = h.token_symbol AND a.address = h.address AND a.date = ?
           JOIN holdings_daily b ON b.token_symbol = h.token_symbol AND b.address = h.address AND b.date = ?
           WHERE h.token_symbol = ? AND COALESCE(h.category, 'whale') = ?""",
        (start, end, symbol, category),
    ).fetchall()
    if not rows:
        return None
    before = sum(r[1] for r in rows)
    after = sum(r[2] for r in rows)
    up = sum(1 for _, a, b in rows if b > a * (1 + _MOVE))
    down = sum(1 for _, a, b in rows if b < a * (1 - _MOVE))
    return {"n": len(rows), "before": before, "after": after,
            "pct": (after - before) / before if before else 0.0, "up": up, "down": down}


def main():
    parser = argparse.ArgumentParser(description="Whale vs insider flow summary")
    parser.add_argument("--windows", default="30,90,365")
    args = parser.parse_args()
    windows = [int(w) for w in args.windows.split(",")]

    con = get_connection()
    latest = con.execute("SELECT MAX(date) FROM holdings_daily").fetchone()[0]
    if not latest:
        sys.exit("No history yet — run scripts/backfill_history.py first")
    supply = total_supplies()
    end = date.fromisoformat(latest)

    print(f"Holdings as of {latest} (first block of the UTC day). Δ% = combined change; "
          f"breadth = members up/down >{_MOVE:.0%}.\n")
    header = f"{'token':<6} {'group':<8} {'n':>3} {'% supply':>8}  " + "  ".join(
        f"{f'{w}d Δ%':>8} {'up/down':>7}" for w in windows)
    print(header)
    print("-" * len(header))
    for symbol in TOKEN_BASKET:
        for category in ("whale", "insider"):
            cells, now = [], None
            for w in windows:
                c = group_change(con, symbol, category, (end - timedelta(days=w)).isoformat(), latest)
                if c is None:
                    cells.append(f"{'—':>8} {'':>7}")
                    continue
                now = c
                cells.append(f"{c['pct']:>+8.1%} {c['up']:>3}/{c['down']:<3}")
            if now is None:
                continue
            share = now["after"] / supply[symbol] if supply.get(symbol) else 0
            print(f"{symbol:<6} {category:<8} {now['n']:>3} {share:>8.1%}  " + "  ".join(cells))
    con.close()
    print("\nThis is today's cohort looking back — wallets that sold out before today aren't included.")


if __name__ == "__main__":
    main()
