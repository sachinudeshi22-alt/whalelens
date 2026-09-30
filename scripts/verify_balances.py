"""
Cross-checks the cohort's Dune-derived balances against on-chain balanceOf().

For each token, reads balanceOf() for every cohort address at the block closest
to when the cohort was pulled (so real trading since then doesn't count as a
mismatch), and flags holders whose Dune balance is off by more than
BALANCE_MISMATCH_TOLERANCE. Results are written to `balance_checks`.

Also reports the wallet type of each current holder, and how much the cohort's
holdings have moved between the pull and the latest block.

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
from chain.rpc import balances_of, block_at_timestamp, latest_block
from chain.classify import classify


def _pull_block(pulled_at: str, cache: dict) -> int:
    ts = calendar.timegm(datetime.strptime(pulled_at, "%Y-%m-%d %H:%M:%S").timetuple())
    if ts not in cache:
        cache[ts] = block_at_timestamp(ts)
    return cache[ts]


def verify_token(symbol: str, token: dict, head: int, block_cache: dict) -> dict:
    con = get_connection()
    rows = con.execute(
        "SELECT address, COALESCE(dune_balance, balance_at_pull), pulled_at "
        "FROM holders WHERE token_symbol = ? ORDER BY rank",
        (symbol,),
    ).fetchall()
    if not rows:
        con.close()
        return {}

    addrs = [r[0] for r in rows]
    dune = {r[0]: r[1] for r in rows}
    block = _pull_block(rows[0][2], block_cache)
    scale = 10 ** token["decimals"]

    at_pull = {a: v / scale for a, v in balances_of(token["contract"], addrs, block).items()}
    now     = {a: v / scale for a, v in balances_of(token["contract"], addrs, head).items()}
    types   = classify(addrs)

    mismatches = []
    for a in addrs:
        chain = at_pull[a]
        rel = (dune[a] - chain) / chain if chain else float("inf")
        con.execute(
            """INSERT OR REPLACE INTO balance_checks
               (token_symbol, address, block_number, dune_balance, chain_balance, rel_diff)
               VALUES (?,?,?,?,?,?)""",
            (symbol, a, block, dune[a], chain, rel if chain else None),
        )
        if abs(rel) > BALANCE_MISMATCH_TOLERANCE:
            mismatches.append((a, dune[a], chain, rel))
    con.commit()
    con.close()

    total_pull = sum(at_pull.values())
    total_now = sum(now.values())
    type_counts = {}
    for t in types.values():
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"\n{symbol}  (pull block {block}, {len(addrs)} holders)")
    print(f"  Dune vs chain mismatches (>{BALANCE_MISMATCH_TOLERANCE:.0%}) : {len(mismatches)}")
    for a, d, c, rel in sorted(mismatches, key=lambda m: -abs(m[3]) if m[2] else float("-inf"))[:5]:
        rel_s = f"{rel:+.1%}" if c else "chain=0"
        print(f"    {a}  dune {d:>18,.2f}  chain {c:>18,.2f}  ({rel_s})")
    print(f"  Wallet types now                   : {type_counts}")
    print(f"  Cohort holdings pull → now         : {total_pull:,.0f} → {total_now:,.0f} "
          f"({(total_now - total_pull) / total_pull:+.1%})")
    zeroed = [a for a in addrs if now[a] == 0]
    if zeroed:
        print(f"  Holders now at zero balance        : {len(zeroed)}")

    return {"symbol": symbol, "mismatches": len(mismatches), "n": len(addrs),
            "types": type_counts, "change": (total_now - total_pull) / total_pull}


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
    print(f"  {'token':<6} {'mismatch':>9}  {'non-EOA now':>11}  {'cohort Δ':>9}")
    for r in summary:
        non_eoa = r["n"] - r["types"].get("eoa", 0)
        print(f"  {r['symbol']:<6} {r['mismatches']:>4}/{r['n']:<4}  {non_eoa:>11}  {r['change']:>+9.1%}")


if __name__ == "__main__":
    main()
