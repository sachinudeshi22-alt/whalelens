"""
Pulls the current top holders for each token in TOKEN_BASKET (from Blockscout or
Dune, per HOLDER_SOURCE),
applies the exclusion filter, verifies every balance on-chain, and writes the
clean cohort to the `holders` table.

Run this once to bootstrap and again weekly to refresh the cohort.
With the Dune source, no pre-saved query is needed — the script creates, runs,
and deletes a temporary query via the Dune API on each run. Raw source rows are
saved to `raw_holder_pulls`, so --from-raw can rebuild cohorts after a filter
change without pulling again.

Usage:
    python scripts/fetch_holders.py
    python scripts/fetch_holders.py --symbol LINK
    python scripts/fetch_holders.py --from-raw          # re-filter the last saved pull
"""
import argparse
import json
import sys
import time
import requests
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    DUNE_API_KEY,
    DUNE_BASE_URL,
    HOLDER_SOURCE,
    EXCLUDE_LABEL_KEYWORDS,
    TOKEN_BASKET,
    TOP_HOLDER_RAW_LIMIT,
    TOP_HOLDER_COHORT_SIZE,
    KNOWN_EXCLUSIONS,
    RESERVED_ADDRESSES,
    BALANCE_MISMATCH_TOLERANCE,
    POSITION_CANDIDATE_LIMIT,
)
from db.schema import init_db, get_connection
from chain.rpc import latest_block
from chain.classify import classify, KEEP_TYPES
from chain.blockscout import metadata_labels, token_holders
from chain.insiders import group_safes, suggest_insider_sources, trace_funding
from chain.positions import POSITION_SOURCES, holdings, position_participants, unseen_position_floor


# ---------------------------------------------------------------------------
# SQL template — values substituted directly; these come from our config,
# never from user input, so string interpolation is safe here.
# ---------------------------------------------------------------------------

# DSToken (MKR) emits Mint(guy, wad) / Burn(guy, wad) instead of Transfer events
# from/to the zero address, so a pure Transfer sum misses every mint and burn.
# Burns matter most: MKR→SKY conversions burn MKR, leaving phantom balances.
_DSTOKEN_MINT = "0f6798a560793a54c3bcfe86a93cde1e73087d944c0ea20544137d4121396885"
_DSTOKEN_BURN = "cc16f5dbb4873280815c1ee09dbd06736cffcc184412cf7a71a0fdb75d397ca5"


def _dstoken_ctes(hex_addr: str) -> tuple[str, str]:
    ctes = f""",
ds_mints AS (
    SELECT bytearray_substring(topic1, 13, 20) AS address,
           CAST(bytearray_to_uint256(data) AS DOUBLE) AS amount
    FROM ethereum.logs
    WHERE contract_address = from_hex('{hex_addr}') AND topic0 = from_hex('{_DSTOKEN_MINT}')
),
ds_burns AS (
    SELECT bytearray_substring(topic1, 13, 20) AS address,
           -CAST(bytearray_to_uint256(data) AS DOUBLE) AS amount
    FROM ethereum.logs
    WHERE contract_address = from_hex('{hex_addr}') AND topic0 = from_hex('{_DSTOKEN_BURN}')
)"""
    unions = """
    UNION ALL
    SELECT * FROM ds_mints
    UNION ALL
    SELECT * FROM ds_burns"""
    return ctes, unions


def _build_sql(contract: str, decimals: int, limit: int, token_standard: str = "erc20") -> str:
    hex_addr = contract.replace("0x", "").lower()
    zero = "0" * 40
    extra_ctes, extra_unions = _dstoken_ctes(hex_addr) if token_standard == "dstoken" else ("", "")
    return f"""
WITH inflows AS (
    SELECT "to" AS address, CAST(value AS DOUBLE) AS amount
    FROM erc20_ethereum.evt_Transfer
    WHERE contract_address = from_hex('{hex_addr}')
),
outflows AS (
    SELECT "from" AS address, -CAST(value AS DOUBLE) AS amount
    FROM erc20_ethereum.evt_Transfer
    WHERE contract_address = from_hex('{hex_addr}')
      AND "from" != from_hex('{zero}')
){extra_ctes},
combined AS (
    SELECT * FROM inflows
    UNION ALL
    SELECT * FROM outflows{extra_unions}
)
SELECT
    '0x' || to_hex(address) AS holder_address,
    SUM(amount) / POWER(10, {decimals}) AS balance
FROM combined
WHERE address != from_hex('{zero}')
GROUP BY address
HAVING SUM(amount) > 0
ORDER BY balance DESC
LIMIT {limit}
"""


# ---------------------------------------------------------------------------
# Dune API helpers
# ---------------------------------------------------------------------------

def _headers():
    return {"X-Dune-API-Key": DUNE_API_KEY, "Content-Type": "application/json"}


def _create_query(sql: str, name: str) -> int:
    """Create a private Dune query and return its query_id."""
    resp = requests.post(
        f"{DUNE_BASE_URL}/query",
        headers=_headers(),
        json={"name": name, "query_sql": sql, "is_private": False},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Dune create query failed {resp.status_code}: {resp.text}")
    return resp.json()["query_id"]


def _execute_query(query_id: int) -> str:
    """Trigger execution of a saved Dune query; return execution_id."""
    resp = requests.post(
        f"{DUNE_BASE_URL}/query/{query_id}/execute",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["execution_id"]


def _poll_until_done(execution_id: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(
            f"{DUNE_BASE_URL}/execution/{execution_id}/status",
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        state = resp.json()["state"]
        if state == "QUERY_STATE_COMPLETED":
            return
        if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            raise RuntimeError(f"Dune execution {state}")
        print(f"  Waiting... ({state})")
        time.sleep(5)
    raise TimeoutError(f"Dune timed out after {timeout}s")


def _fetch_results(execution_id: str) -> list[dict]:
    resp = requests.get(
        f"{DUNE_BASE_URL}/execution/{execution_id}/results",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["result"]["rows"]


def _delete_query(query_id: int) -> None:
    """Best-effort cleanup of the temporary query."""
    try:
        requests.delete(
            f"{DUNE_BASE_URL}/query/{query_id}",
            headers=_headers(),
            timeout=15,
        )
    except Exception:
        pass  # non-fatal; user can delete manually from dune.com if needed


def fetch_top_holders_from_dune(symbol: str, token: dict) -> list[dict]:
    """
    Build SQL, create a temp Dune query, run it, return rows, clean up.
    Each row: {holder_address: str, balance: float}
    """
    sql = _build_sql(token["contract"], token["decimals"], TOP_HOLDER_RAW_LIMIT,
                     token.get("standard", "erc20"))
    query_name = f"WhaleLens_{symbol}_top_holders_tmp"

    print(f"  Creating temporary Dune query '{query_name}'...")
    query_id = _create_query(sql, query_name)
    print(f"  Query ID: {query_id}")

    try:
        print(f"  Executing...")
        execution_id = _execute_query(query_id)
        print(f"  Execution ID: {execution_id}")
        _poll_until_done(execution_id)
        rows = _fetch_results(execution_id)
        print(f"  Dune returned {len(rows)} raw rows")
        return rows
    finally:
        _delete_query(query_id)
        print(f"  Cleaned up temp query {query_id}")


# ---------------------------------------------------------------------------
# Blockscout holder source (free, no key)
# ---------------------------------------------------------------------------

def fetch_top_holders_from_blockscout(symbol: str, token: dict) -> list[dict]:
    """
    Top holders by current wallet balance.
    Each row: {holder_address: str, balance: float, labels: list[str]}
    """
    scale = 10 ** token["decimals"]
    rows = [{"holder_address": r["address"], "balance": r["value"] / scale, "labels": r["labels"]}
            for r in token_holders(token["contract"], TOP_HOLDER_RAW_LIMIT)]
    print(f"  Blockscout returned {len(rows)} raw rows")
    return rows


def fetch_top_holders(symbol: str, token: dict) -> list[dict]:
    if HOLDER_SOURCE == "blockscout":
        return fetch_top_holders_from_blockscout(symbol, token)
    return fetch_top_holders_from_dune(symbol, token)


def add_position_candidates(symbol: str, token: dict, rows: list[dict],
                            positions: dict[str, dict[str, int]]) -> tuple[list[dict], float]:
    """
    Append the largest staked-position holders that aren't already in the wallet list.
    Returns (rows, position_floor) where position_floor is the smallest position
    considered — used by the completeness check.
    """
    if not positions:
        return rows, 0.0
    scale = 10 ** token["decimals"]
    ranked = sorted(positions.items(), key=lambda kv: -sum(kv[1].values()))[:POSITION_CANDIDATE_LIMIT]
    floor = sum(ranked[-1][1].values()) / scale if len(ranked) == POSITION_CANDIDATE_LIMIT else 0.0
    seen = {r["holder_address"] for r in rows}
    new = [addr for addr, _ in ranked if addr not in seen]
    print(f"  Adding {len(new)} staked-position holders (labels from Blockscout metadata)...")
    labels = metadata_labels(new)
    for addr in new:
        rows.append({"holder_address": addr, "balance": None, "labels": labels[addr]})
    return rows, floor


# ---------------------------------------------------------------------------
# Exclusion filter — shows full funnel
# ---------------------------------------------------------------------------

_EXCLUSION_SET = {a.lower() for a in KNOWN_EXCLUSIONS} | {a.lower() for a in RESERVED_ADDRESSES}


def _excluded_by_label(labels: list[str]) -> str | None:
    """Return the first label matching EXCLUDE_LABEL_KEYWORDS, else None."""
    for label in labels:
        low = label.lower()
        if any(k in low for k in EXCLUDE_LABEL_KEYWORDS):
            return label
    return None


def _preview(entries: list[dict]) -> str:
    if not entries:
        return ""
    more = "..." if len(entries) > 3 else ""
    return f"  ({', '.join(h['holder_address'][:10] + '…' for h in entries[:3])}{more})"


def apply_exclusion_filter(rows: list[dict], symbol: str, token: dict,
                           positions: dict[str, dict[str, int]] | None = None,
                           position_floor: float = 0.0) -> tuple[list[dict], list[dict]]:
    """
    Five-stage filter with funnel output. Returns (whales, insiders).
      1. Drop KNOWN_EXCLUSIONS and exchange wallets (labels match EXCLUDE_LABEL_KEYWORDS)
      2. Classify by bytecode: keep EOAs, EIP-7702 EOAs and Safes; drop other contracts
      3. Verify holdings on-chain at one pinned block — balanceOf() plus staked positions
         (chain/positions.py); drop zero holdings and re-rank by the on-chain total
      4. Trace funding in rank order and split out insiders (chain/insiders.py),
         until TOP_HOLDER_COHORT_SIZE whales are found
      5. Whales = top N non-insiders; insiders ranked above the last whale are kept separately
    Any RPC failure raises — a partial check must never pass contracts through.
    """
    positions = positions or {}

    # Stage 1: known addresses and exchange labels
    after_labels, dropped_labels, dropped_tagged = [], [], []
    for rank, row in enumerate(rows, start=1):
        addr = row["holder_address"].lower()
        entry = {"holder_address": addr, "rank": rank if row["balance"] is not None else None,
                 "source_balance": row["balance"], "labels": row.get("labels") or []}
        if addr in _EXCLUSION_SET:
            dropped_labels.append(entry)
        elif _excluded_by_label(entry["labels"]):
            dropped_tagged.append(entry)
        else:
            after_labels.append(entry)

    # Stage 2: wallet-type classification
    print(f"  Classifying {len(after_labels)} addresses by bytecode...")
    types = classify([h["holder_address"] for h in after_labels])
    after_types, dropped_contracts = [], []
    for h in after_labels:
        h["wallet_type"] = types[h["holder_address"]]
        (after_types if h["wallet_type"] in KEEP_TYPES else dropped_contracts).append(h)

    # Stage 3: on-chain holdings (wallet + staked positions)
    block = latest_block()
    print(f"  Verifying {len(after_types)} holdings on-chain at block {block}...")
    held = holdings(symbol, token, [h["holder_address"] for h in after_types], block, positions)
    verified, phantoms, mismatched = [], [], []
    for h in after_types:
        x = held[h["holder_address"]]
        h.update(wallet_balance=x["wallet"], positions=x["positions"], balance=x["total"],
                 verified_block=block)
        if h["balance"] <= 0:
            phantoms.append(h)
            continue
        src = h["source_balance"]
        if src is not None and x["wallet"] > 0 and abs(src - x["wallet"]) / x["wallet"] > BALANCE_MISMATCH_TOLERANCE:
            mismatched.append(h)
        verified.append(h)
    verified.sort(key=lambda h: h["balance"], reverse=True)

    # Stage 4: insider tracing, in rank order, until the whale cohort is full
    print(f"  Tracing first-inflow funding to separate insiders...")
    traced: list[dict] = []
    whales: list[dict] = []
    i = 0
    while len(whales) < TOP_HOLDER_COHORT_SIZE and i < len(verified):
        batch_ = verified[i:i + 10]
        i += len(batch_)
        for h in batch_:
            trace_funding(symbol, token, h)
            traced.append(h)
        group_safes(traced)
        whales = [h for h in traced if not h.get("insider_reason")]

    # Stage 5: final split
    whales = whales[:TOP_HOLDER_COHORT_SIZE]
    cutoff = whales[-1]["balance"] if whales else 0
    insiders = [h for h in traced if h.get("insider_reason") and h["balance"] >= cutoff]
    for h in whales:
        h["category"] = "whale"
    for h in insiders:
        h["category"] = "insider"

    type_counts: dict[str, int] = {}
    for h in whales:
        type_counts[h["wallet_type"]] = type_counts.get(h["wallet_type"], 0) + 1
    staked = sum(1 for h in whales if h["positions"])

    # Funnel output
    print(f"\n  Filter funnel for {symbol}:")
    print(f"    Raw candidates                : {len(rows):>4}")
    print(f"    Dropped — known addresses     : {len(dropped_labels):>4}{_preview(dropped_labels)}")
    print(f"    Dropped — exchange labels     : {len(dropped_tagged):>4}", end="")
    if dropped_tagged:
        tags = sorted({_excluded_by_label(h["labels"]) for h in dropped_tagged})
        print(f"  ({', '.join(tags[:4])}{'...' if len(tags) > 4 else ''})", end="")
    print()
    print(f"    Dropped — non-wallet contracts: {len(dropped_contracts):>4}{_preview(dropped_contracts)}")
    print(f"    Dropped — zero on-chain       : {len(phantoms):>4}{_preview(phantoms)}")
    print(f"    Source/chain mismatch >{BALANCE_MISMATCH_TOLERANCE:.0%} (kept, chain value used): {len(mismatched)}")
    print(f"    Real wallets remaining        : {len(verified):>4}")
    print(f"    Insiders split out            : {len(insiders):>4}")
    for h in insiders[:5]:
        print(f"      {h['holder_address'][:12]}… {h['balance']:>18,.0f}  {h['insider_reason']}")
    print(f"    Whale cohort (top {TOP_HOLDER_COHORT_SIZE})         : {len(whales):>4}  {type_counts}"
          + (f", {staked} with staked positions" if positions else ""))
    entities = {h["entity_id"] for h in whales if h.get("entity_id")}
    if entities:
        grouped = sum(1 for h in whales if h.get("entity_id"))
        print(f"    Safes grouped by shared signers: {grouped} whales in {len(entities)} entities")

    # Sources over-report (phantom balances) far more often than they under-report,
    # so any real holder missing from the candidate lists holds at most the last raw
    # wallet balance plus the smallest position considered. If the smallest whale
    # beats that, nobody outside the lists could displace it.
    wallet_rows = [r for r in rows if r["balance"] is not None]
    if whales and wallet_rows:
        floor = wallet_rows[-1]["balance"] + position_floor
        if whales[-1]["balance"] >= floor:
            print(f"    Completeness: OK (cohort min {whales[-1]['balance']:,.2f} ≥ max unseen {floor:,.2f})")
        else:
            print(f"\n  WARNING: cohort min {whales[-1]['balance']:,.2f} < max unseen holding {floor:,.2f} — "
                  f"holders beyond the candidate lists may be missing; raise the limits")

    for funder, n in suggest_insider_sources(symbol, traced):
        print(f"  REVIEW: {funder} was the first funder of {n} top holders — possible insider source")

    if len(whales) < TOP_HOLDER_COHORT_SIZE:
        print(f"\n  WARNING: only {len(whales)} whales found — consider increasing TOP_HOLDER_RAW_LIMIT")
    if len(phantoms) + len(mismatched) > len(after_types) * 0.2:
        print(f"\n  NOTE: {len(phantoms) + len(mismatched)}/{len(after_types)} source balances for {symbol} "
              f"were wrong and replaced with on-chain values — the source misses a non-standard event")

    return whales, insiders


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def upsert_token(symbol: str, token: dict) -> None:
    con = get_connection()
    con.execute(
        """
        INSERT INTO tokens (symbol, contract, decimals, coingecko_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            contract     = excluded.contract,
            decimals     = excluded.decimals,
            coingecko_id = excluded.coingecko_id
        """,
        (symbol, token["contract"], token["decimals"], token.get("coingecko_id")),
    )
    con.commit()
    con.close()


def save_raw_pull(symbol: str, pull_id: str, rows: list[dict]) -> None:
    con = get_connection()
    con.executemany(
        "INSERT OR IGNORE INTO raw_holder_pulls "
        "(token_symbol, pull_id, source, rank, address, source_balance, labels) VALUES (?,?,?,?,?,?,?)",
        [(symbol, pull_id, HOLDER_SOURCE, i, r["holder_address"].lower(), r["balance"],
          json.dumps(r.get("labels") or [])) for i, r in enumerate(rows, 1)],
    )
    con.commit()
    con.close()


def load_latest_raw_pull(symbol: str) -> list[dict]:
    con = get_connection()
    rows = con.execute(
        """SELECT address, source_balance, labels FROM raw_holder_pulls
           WHERE token_symbol = ? AND pull_id = (
               SELECT MAX(pull_id) FROM raw_holder_pulls WHERE token_symbol = ?)
           ORDER BY rank""",
        (symbol, symbol),
    ).fetchall()
    con.close()
    return [{"holder_address": a, "balance": b, "labels": json.loads(l or "[]")} for a, b, l in rows]


def replace_cohort(symbol: str, holders: list[dict]) -> None:
    con = get_connection()
    try:
        con.execute("DELETE FROM holders WHERE token_symbol = ?", (symbol,))
        con.executemany(
            """INSERT INTO holders
               (token_symbol, address, rank, balance_at_pull, wallet_type, source_balance,
                verified_block, labels, wallet_balance, positions, category, insider_reason,
                first_funder, entity_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(symbol, h["holder_address"], h["rank"], h["balance"], h["wallet_type"],
              h["source_balance"], h["verified_block"], json.dumps(h["labels"]),
              h["wallet_balance"], json.dumps(h["positions"]), h["category"],
              h.get("insider_reason"), h.get("first_funder"), h.get("entity_id")) for h in holders],
        )
        con.commit()
        n_ins = sum(1 for h in holders if h["category"] == "insider")
        print(f"  Wrote {len(holders) - n_ins} whales + {n_ins} insiders to DB for {symbol}")
    except sqlite3.Error as e:
        con.rollback()
        raise RuntimeError(f"DB write failed: {e}") from e
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_token(symbol: str, token: dict, from_raw: bool, pull_id: str) -> None:
    print(f"\n{'='*60}")
    print(f"Token: {symbol}  ({token['contract']})")
    print(f"{'='*60}")

    if from_raw:
        raw_rows = load_latest_raw_pull(symbol)
        if not raw_rows:
            print("  No saved raw pull — run without --from-raw first")
            return
        print(f"  Loaded {len(raw_rows)} rows from the last saved pull")
    else:
        raw_rows = fetch_top_holders(symbol, token)
        save_raw_pull(symbol, pull_id, raw_rows)

    positions = {}
    position_floor = 0.0
    if symbol in POSITION_SOURCES:
        print(f"  Reading staked positions ({', '.join(s.name for s in POSITION_SOURCES[symbol])})...")
        positions = position_participants(symbol)
        print(f"  {len(positions)} addresses hold staked {symbol}")
        raw_rows, position_floor = add_position_candidates(symbol, token, raw_rows, positions)
        position_floor += unseen_position_floor(symbol)

    whales, insiders = apply_exclusion_filter(raw_rows, symbol, token, positions, position_floor)

    if not whales:
        print("  WARNING: no clean holders after filtering — broaden exclusion list or check source results")
        return

    upsert_token(symbol, token)
    replace_cohort(symbol, whales + insiders)

    print(f"\n  Top 10 whales:")
    for h in whales[:10]:
        pos = f"  (staked {sum(h['positions'].values()):,.0f})" if h["positions"] else ""
        print(f"    {h['holder_address']}  {h['balance']:>20,.4f} {symbol}  [{h['wallet_type']}]{pos}")


def main():
    parser = argparse.ArgumentParser(description="Fetch, filter and verify top holders")
    parser.add_argument("--symbol", default=None, help="Single token symbol (e.g. LINK)")
    parser.add_argument("--from-raw", action="store_true",
                        help="Re-filter the last saved pull instead of fetching again")
    args = parser.parse_args()

    if HOLDER_SOURCE == "dune" and not DUNE_API_KEY and not args.from_raw:
        sys.exit("ERROR: DUNE_API_KEY not set in .env")

    init_db()

    if args.symbol:
        if args.symbol not in TOKEN_BASKET:
            sys.exit(f"ERROR: '{args.symbol}' not in TOKEN_BASKET")
        basket = {args.symbol: TOKEN_BASKET[args.symbol]}
    else:
        basket = TOKEN_BASKET

    pull_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for symbol, token in basket.items():
        try:
            process_token(symbol, token, args.from_raw, pull_id)
        except (requests.RequestException, RuntimeError, TimeoutError) as e:
            print(f"  FAILED for {symbol}: {e}")
        if not args.from_raw and HOLDER_SOURCE == "dune":
            time.sleep(15)  # Dune free-tier rate limiting — conservative to avoid 429s


if __name__ == "__main__":
    main()
