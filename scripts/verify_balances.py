"""
Cross-checks the cohort's source-reported balances against on-chain balanceOf().

For each token, reads balanceOf() for every cohort address at the block closest
to when the cohort was pulled (so real trading since then doesn't count as a
mismatch), and flags holders whose source balance is off by more than
BALANCE_MISMATCH_TOLERANCE. Results are written to `balance_checks`.

Also reports the wallet type of each current holder, and how much whale and
insider holdings (wallet + staked positions) moved between the pull and now.

Usage:
    python scripts/verify_balances.py
    python scripts/verify_balances.py --symbol MKR
"""
import argparse
import calendar
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TOKEN_BASKET, BALANCE_MISMATCH_TOLERANCE
from db.schema import init_db, get_connection
from chain.rpc import block_at_timestamp, latest_block
from chain.positions import holdings
from chain.classify import classify


def _pull_block(pulled_at: str, cache: dict) -> int:
    ts = calendar.timegm(datetime.strptime(pulled_at, "%Y-%m-%d %H:%M:%S").timetuple())
    if ts not in cache:
        cache[ts] = block_at_timestamp(ts)
    return cache[ts]


def verify_token(symbol: str, token: dict, head: int, block_cache: dict) -> dict:
    con = get_connection()
    rows = con.execute(
        "SELECT address, source_balance, pulled_at, verified_block, COALESCE(category, 'whale') "
        "FROM holders WHERE token_symbol = ? ORDER BY balance_at_pull DESC",
        (symbol,),
    ).fetchall()
    if not rows:
        con.close()
        return {}

    addrs = [r[0] for r in rows]
    source = {r[0]: r[1] for r in rows}
    category = {r[0]: r[4] for r in rows}
    block = rows[0][3] or _pull_block(rows[0][2], block_cache)

    # Staked positions count as holdings, so staking isn't mistaken for selling
    at_pull = holdings(symbol, token, addrs, block)
    now     = holdings(symbol, token, addrs, head)
    types   = classify(addrs)

    mismatches = []
    for a in addrs:
        if source[a] is None:
            continue   # candidate came from a position list, not the holder source
        chain = at_pull[a]["wallet"]
        rel = (source[a] - chain) / chain if chain else float("inf")
        con.execute(
            """INSERT OR REPLACE INTO balance_checks
               (token_symbol, address, block_number, source_balance, chain_balance, rel_diff)
               VALUES (?,?,?,?,?,?)""",
            (symbol, a, block, source[a], chain, rel if chain else None),
        )
        if abs(rel) > BALANCE_MISMATCH_TOLERANCE:
            mismatches.append((a, source[a], chain, rel))
    con.commit()
    con.close()

    def change(cat):
        members = [a for a in addrs if category[a] == cat]
        before = sum(at_pull[a]["total"] for a in members)
        after = sum(now[a]["total"] for a in members)
        return len(members), before, after, ((after - before) / before if before else 0.0)

    type_counts = {}
    for t in types.values():
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"\n{symbol}  (pull block {block}, {len(addrs)} holders)")
    print(f"  Source vs chain wallet mismatches (>{BALANCE_MISMATCH_TOLERANCE:.0%}) : {len(mismatches)}")
    for a, d, c, rel in sorted(mismatches, key=lambda m: -abs(m[3]) if m[2] else float("-inf"))[:5]:
        rel_s = f"{rel:+.1%}" if c else "chain=0"
        print(f"    {a}  source {d:>18,.2f}  chain {c:>18,.2f}  ({rel_s})")
    print(f"  Wallet types now                   : {type_counts}")
    result = {"symbol": symbol, "mismatches": len(mismatches), "n": len(addrs), "types": type_counts}
    for cat in ("whale", "insider"):
        n, before, after, pct = change(cat)
        result[cat] = pct if n else None
        if n:
            print(f"  {cat.capitalize() + 's':<9}({n:>2}) holdings pull → now: {before:,.0f} → {after:,.0f} ({pct:+.1%})")
    zeroed = [a for a in addrs if now[a]["total"] == 0]
    if zeroed:
        print(f"  Holders now at zero holdings       : {len(zeroed)}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Verify cohort balances on-chain")
    parser.add_argument("--symbol", default=None)
    args = parser.parse_args()

    init_db()
    basket = {args.symbol: TOKEN_BASKET[args.symbol]} if args.symbol else TOKEN_BASKET
    head = latest_block()
    block_cache: dict = {}

    summary = []
    for symbol, token in basket.items():
        result = verify_token(symbol, token, head, block_cache)
        if result:
            summary.append(result)

    print(f"\n{'='*60}\nSummary (latest block {head})\n{'='*60}")
    print(f"  {'token':<6} {'mismatch':>9}  {'non-EOA now':>11}  {'whales Δ':>9}  {'insiders Δ':>10}")
    fmt = lambda v: f"{v:+.1%}" if v is not None else "—"
    for r in summary:
        non_eoa = r["n"] - r["types"].get("eoa", 0)
        print(f"  {r['symbol']:<6} {r['mismatches']:>4}/{r['n']:<4}  {non_eoa:>11}  "
              f"{fmt(r['whale']):>9}  {fmt(r['insider']):>10}")


if __name__ == "__main__":
    main()
