"""
Builds point-in-time cohorts: the top whales (and insiders) as of each date,
instead of today's top holders looked at backwards.

Why: looking back at today's top 50 builds in accumulation — they're the top
50 *because* they ended up holding the most. Selecting the cohort at the start
of each window removes that look-ahead bias.

How (per token):
  1. Universe = latest raw top-holder list + every address that ever staked
     (position sources) + every address that sent at least UNIVERSE_SENT_FRACTION
     of today's smallest whale holding during the scanned period (transfer_totals).
     Staking participants come from chain/positions.py; those below its deposit
     floor are covered by adding the floor to the bound.
  2. Same exclusions as the live cohort (known addresses, exchange labels,
     non-wallet contracts), plus high-activity addresses (exchange / market-maker
     behaviour, see HIGH_ACTIVITY_TRANSFERS).
  3. Daily holdings for the whole universe (holdings_daily).
  4. For each date: rank by holdings, trace insiders among the top, and store
     the top N non-insiders as that date's whales (cohort_daily).

Completeness: an address outside the universe holds at most F + T on any date,
where F is today's smallest raw-list wallet balance and T the sent threshold
(its holdings then = holdings now + net outflow since, and both are bounded).
If a date's Nth-largest whale holds more than F + T, nobody outside the
universe could have made that date's cohort. Each date's result is stored in
cohort_quality.

Usage:
    python scripts/build_cohorts.py
    python scripts/build_cohorts.py --symbol LINK
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    TOKEN_BASKET, TOP_HOLDER_COHORT_SIZE, KNOWN_EXCLUSIONS, RESERVED_ADDRESSES,
    UNIVERSE_SENT_FRACTION, HIGH_ACTIVITY_TRANSFERS,
)
from db.schema import init_db, get_connection
from chain.blockscout import metadata_labels
from chain.classify import classify, KEEP_TYPES
from chain.insiders import group_safes, trace_funding
from chain.positions import POSITION_SOURCES
from backfill_history import record_day
from fetch_holders import _excluded_by_label

_EXCLUSION_SET = {a.lower() for a in KNOWN_EXCLUSIONS} | {a.lower() for a in RESERVED_ADDRESSES}


def build_universe(con, symbol: str) -> tuple[dict[str, dict], float, float]:
    """Returns ({address: info}, F, T)."""
    raw = con.execute(
        """SELECT address, source_balance, labels FROM raw_holder_pulls
           WHERE token_symbol = ? AND pull_id = (SELECT MAX(pull_id) FROM raw_holder_pulls WHERE token_symbol = ?)
           ORDER BY rank""", (symbol, symbol)).fetchall()
    if not raw:
        raise RuntimeError(f"no raw pull for {symbol}")
    floor_wallet = raw[-1][1]
    min_whale = con.execute("SELECT MIN(balance_at_pull) FROM holders WHERE token_symbol = ? AND category = 'whale'",
                            (symbol,)).fetchone()[0]
    base = UNIVERSE_SENT_FRACTION * min_whale
    # Stakers join only if their deposits could reach `base`; a staker left out holds at
    # most unseen_above(base) staked, which joins the bound alongside the sent threshold
    sent_threshold = base + sum(src.unseen_above(base) for src in POSITION_SOURCES.get(symbol, []))

    uni: dict[str, dict] = {}
    for addr, _, labels in raw:
        uni[addr] = {"why": "top holder list", "labels": json.loads(labels or "[]")}
    for source in POSITION_SOURCES.get(symbol, []):
        for owner in source.participants(min_position=base):
            uni.setdefault(owner, {"why": f"{source.name} participant", "labels": None})
    for addr, sent in con.execute("SELECT address, sent FROM transfer_totals WHERE token_symbol = ? AND sent >= ?",
                                  (symbol, sent_threshold)):
        uni.setdefault(addr, {"why": f"sent {sent:,.0f} in period", "labels": None})

    unlabelled = [a for a, v in uni.items() if v["labels"] is None]
    if unlabelled:
        labels = metadata_labels(unlabelled)
        for a in unlabelled:
            uni[a]["labels"] = labels[a]
    return uni, floor_wallet, sent_threshold


def filter_universe(con, symbol: str, uni: dict[str, dict]) -> list[str]:
    activity = {a: n for a, n in con.execute(
        "SELECT address, n_out + n_in FROM transfer_totals WHERE token_symbol = ?", (symbol,))}
    candidates, counts = [], {"known": 0, "exchange label": 0, "high activity": 0, "contract": 0}
    for addr, info in uni.items():
        if addr in RESERVED_ADDRESSES:
            info["excluded"] = RESERVED_ADDRESSES[addr]; counts["known"] += 1
        elif addr in _EXCLUSION_SET:
            info["excluded"] = "known address"; counts["known"] += 1
        elif _excluded_by_label(info["labels"]):
            info["excluded"] = f"exchange label '{_excluded_by_label(info['labels'])}'"; counts["exchange label"] += 1
        elif activity.get(addr, 0) >= HIGH_ACTIVITY_TRANSFERS:
            info["excluded"] = f"high activity ({activity[addr]:,} transfers in period)"; counts["high activity"] += 1
        else:
            candidates.append(addr)
    types = classify(candidates)
    kept = []
    for addr in candidates:
        uni[addr]["wallet_type"] = types[addr]
        if types[addr] in KEEP_TYPES:
            kept.append(addr)
        else:
            uni[addr]["excluded"] = "non-wallet contract"; counts["contract"] += 1
    print(f"  Universe {len(uni):,} → {len(kept):,} wallets  (excluded: {counts})")
    return kept


def rank_dates(con, symbol: str, kept: list[str]) -> dict[str, list[tuple[str, float]]]:
    placeholders = ",".join("?" * len(kept))
    by_date: dict[str, list[tuple[str, float]]] = {}
    for date, addr, total in con.execute(
            f"SELECT date, address, total FROM holdings_daily WHERE token_symbol = ? AND address IN ({placeholders})",
            (symbol, *kept)):
        if total > 0:
            by_date.setdefault(date, []).append((addr, total))
    for rows in by_date.values():
        rows.sort(key=lambda r: -r[1])
    return by_date


def build_token(symbol: str, token: dict) -> None:
    con = get_connection()
    print(f"\n{symbol}")
    uni, floor_wallet, sent_threshold = build_universe(con, symbol)
    kept = filter_universe(con, symbol, uni)

    dates = [r[0] for r in con.execute("SELECT date FROM daily_blocks ORDER BY date")]
    blocks = dict(con.execute("SELECT date, block_number FROM daily_blocks"))
    con.close()
    print(f"  Recording daily holdings for {len(kept):,} wallets × {len(dates)} days...")
    for d in dates:
        record_day(symbol, token, d, blocks[d], kept)

    con = get_connection()
    by_date = rank_dates(con, symbol, kept)

    # Trace insiders among each date's top until no date's top N changes
    traced: dict[str, dict] = {}
    n = TOP_HOLDER_COHORT_SIZE
    while True:
        need = set()
        for rows in by_date.values():
            whales = 0
            for addr, _ in rows:
                if addr not in traced:
                    need.add(addr)
                    whales += 1
                elif not traced[addr].get("insider_reason"):
                    whales += 1
                if whales >= n:
                    break
        if not need:
            break
        print(f"  Tracing funding for {len(need)} more addresses...")
        for addr in need:
            h = {"holder_address": addr, "labels": uni[addr]["labels"],
                 "wallet_type": uni[addr].get("wallet_type")}
            trace_funding(symbol, token, h)
            traced[addr] = h
        group_safes(list(traced.values()))

    rows_out, quality = [], []
    bound = floor_wallet + sent_threshold
    for date, rows in by_date.items():
        whales, insiders = [], []
        for addr, total in rows:
            reason = traced.get(addr, {}).get("insider_reason")
            if reason:
                insiders.append((addr, total))
            elif len(whales) < n:
                whales.append((addr, total))
            if len(whales) >= n:
                break
        cutoff = whales[-1][1] if whales else 0
        rows_out += [(symbol, date, a, "whale", i + 1, t) for i, (a, t) in enumerate(whales)]
        rows_out += [(symbol, date, a, "insider", None, t) for a, t in insiders if t >= cutoff]
        quality.append((symbol, date, cutoff, bound, int(cutoff > bound and len(whales) == n)))

    con.execute("DELETE FROM cohort_daily WHERE token_symbol = ?", (symbol,))
    con.executemany("INSERT INTO cohort_daily (token_symbol, date, address, category, rank, total) "
                    "VALUES (?,?,?,?,?,?)", rows_out)
    con.execute("DELETE FROM cohort_quality WHERE token_symbol = ?", (symbol,))
    con.executemany("INSERT INTO cohort_quality (token_symbol, date, nth_whale_total, outside_bound, complete) "
                    "VALUES (?,?,?,?,?)", quality)
    con.execute("DELETE FROM universe WHERE token_symbol = ?", (symbol,))
    con.executemany("INSERT INTO universe (token_symbol, address, why, excluded, wallet_type, labels, "
                    "insider_reason, entity_id) VALUES (?,?,?,?,?,?,?,?)",
                    [(symbol, a, v["why"], v.get("excluded"), v.get("wallet_type"), json.dumps(v["labels"]),
                      traced.get(a, {}).get("insider_reason"), traced.get(a, {}).get("entity_id"))
                     for a, v in uni.items()])
    con.commit()

    complete = sum(q[4] for q in quality)
    print(f"  Cohorts for {len(quality)} dates; completeness proven on {complete}/{len(quality)} "
          f"(bound {bound:,.0f} = raw floor {floor_wallet:,.0f} + sent threshold {sent_threshold:,.0f})")
    if complete < len(quality):
        worst = min(quality, key=lambda q: q[2] - q[3])
        print(f"  Weakest date {worst[1]}: Nth whale {worst[2]:,.0f} vs bound {worst[3]:,.0f}")
    con.close()


def main():
    parser = argparse.ArgumentParser(description="Build point-in-time whale cohorts")
    parser.add_argument("--symbol", default=None)
    args = parser.parse_args()
    init_db()
    con = get_connection()
    last_day_block = con.execute("SELECT MAX(block_number) FROM daily_blocks").fetchone()[0]
    scanned = {r[0] for r in con.execute("SELECT token_symbol FROM scan_state WHERE next_block > ?",
                                         (last_day_block,))}
    con.close()
    basket = {args.symbol: TOKEN_BASKET[args.symbol]} if args.symbol else TOKEN_BASKET
    for symbol, token in basket.items():
        if symbol not in scanned:
            print(f"\n{symbol}: transfer scan incomplete — run scan_transfers.py first")
            continue
        build_token(symbol, token)


if __name__ == "__main__":
    main()
