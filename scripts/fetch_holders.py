"""
Pulls the current top holders for each token in TOKEN_BASKET from Dune,
applies the exclusion filter, verifies every balance on-chain, and writes the
clean cohort to the `holders` table.

Run this once to bootstrap and again weekly to refresh the cohort.
No pre-saved Dune query needed — the script creates, runs, and deletes a
temporary query via the Dune API on each run. Raw Dune rows are saved to
`raw_holder_pulls`, so --from-raw can rebuild cohorts after a filter change
without spending Dune credits.

Usage:
    python scripts/fetch_holders.py
    python scripts/fetch_holders.py --symbol LINK
    python scripts/fetch_holders.py --from-raw          # re-filter the last saved pull
"""
import argparse
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
    TOKEN_BASKET,
    TOP_HOLDER_RAW_LIMIT,
    TOP_HOLDER_COHORT_SIZE,
    KNOWN_EXCLUSIONS,
    BALANCE_MISMATCH_TOLERANCE,
)
from db.schema import init_db, get_connection
from chain.rpc import balances_of, latest_block
from chain.classify import classify, KEEP_TYPES


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
# Exclusion filter — shows full funnel
# ---------------------------------------------------------------------------

_EXCLUSION_SET = {a.lower() for a in KNOWN_EXCLUSIONS}


def _preview(entries: list[dict]) -> str:
    if not entries:
        return ""
    more = "..." if len(entries) > 3 else ""
    return f"  ({', '.join(h['holder_address'][:10] + '…' for h in entries[:3])}{more})"


def apply_exclusion_filter(rows: list[dict], symbol: str, token: dict) -> list[dict]:
    """
    Four-stage filter with funnel output:
      1. Drop known exchanges / labelled addresses (KNOWN_EXCLUSIONS)
      2. Classify by bytecode: keep EOAs, EIP-7702 EOAs and Safes; drop other contracts
      3. Verify balances with balanceOf() at one pinned block; drop phantom (zero) balances
         and re-rank by the on-chain balance, which is the source of truth
      4. Keep top N of what remains
    Any RPC failure raises — a partial check must never pass contracts through.
    """
    # Stage 1: known label exclusions
    after_labels, dropped_labels = [], []
    for rank, row in enumerate(rows, start=1):
        addr = row["holder_address"].lower()
        entry = {"holder_address": addr, "rank": rank, "dune_balance": row["balance"]}
        (dropped_labels if addr in _EXCLUSION_SET else after_labels).append(entry)

    # Stage 2: wallet-type classification
    print(f"  Classifying {len(after_labels)} addresses by bytecode...")
    types = classify([h["holder_address"] for h in after_labels])
    after_types, dropped_contracts = [], []
    for h in after_labels:
        h["wallet_type"] = types[h["holder_address"]]
        (after_types if h["wallet_type"] in KEEP_TYPES else dropped_contracts).append(h)

    # Stage 3: on-chain balance verification
    block = latest_block()
    print(f"  Verifying {len(after_types)} balances with balanceOf() at block {block}...")
    scale = 10 ** token["decimals"]
    chain = balances_of(token["contract"], [h["holder_address"] for h in after_types], block)
    verified, phantoms, mismatched = [], [], []
    for h in after_types:
        h["balance"] = chain[h["holder_address"]] / scale
        h["verified_block"] = block
        if h["balance"] <= 0:
            phantoms.append(h)
            continue
        if abs(h["dune_balance"] - h["balance"]) / h["balance"] > BALANCE_MISMATCH_TOLERANCE:
            mismatched.append(h)
        verified.append(h)
    verified.sort(key=lambda h: h["balance"], reverse=True)

    # Stage 4: cap to cohort size
    cohort = verified[:TOP_HOLDER_COHORT_SIZE]

    type_counts = {}
    for h in cohort:
        type_counts[h["wallet_type"]] = type_counts.get(h["wallet_type"], 0) + 1

    # Funnel output
    print(f"\n  Filter funnel for {symbol}:")
    print(f"    Raw pulled from Dune          : {len(rows):>4}")
    print(f"    Dropped — known labels        : {len(dropped_labels):>4}{_preview(dropped_labels)}")
    print(f"    Dropped — non-wallet contracts: {len(dropped_contracts):>4}{_preview(dropped_contracts)}")
    print(f"    Dropped — zero on-chain       : {len(phantoms):>4}{_preview(phantoms)}")
    print(f"    Dune/chain mismatch >{BALANCE_MISMATCH_TOLERANCE:.0%} (kept, chain value used): {len(mismatched)}")
    print(f"    Real wallets remaining        : {len(verified):>4}")
    print(f"    Kept in cohort (top {TOP_HOLDER_COHORT_SIZE})       : {len(cohort):>4}  {type_counts}")

    if len(cohort) < TOP_HOLDER_COHORT_SIZE:
        print(f"\n  WARNING: only {len(cohort)} clean holders found — consider increasing TOP_HOLDER_RAW_LIMIT")
    if len(phantoms) + len(mismatched) > len(after_types) * 0.2:
        print(f"\n  WARNING: >20% of Dune balances are wrong for {symbol} — the transfer-sum "
              f"query likely misses a non-standard event; ranking may be missing real holders")

    return cohort


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
        "INSERT OR IGNORE INTO raw_holder_pulls (token_symbol, pull_id, rank, address, dune_balance) "
        "VALUES (?,?,?,?,?)",
        [(symbol, pull_id, i, r["holder_address"].lower(), r["balance"]) for i, r in enumerate(rows, 1)],
    )
    con.commit()
    con.close()


def load_latest_raw_pull(symbol: str) -> list[dict]:
    con = get_connection()
    rows = con.execute(
        """SELECT address, dune_balance FROM raw_holder_pulls
           WHERE token_symbol = ? AND pull_id = (
               SELECT MAX(pull_id) FROM raw_holder_pulls WHERE token_symbol = ?)
           ORDER BY rank""",
        (symbol, symbol),
    ).fetchall()
    con.close()
    return [{"holder_address": a, "balance": b} for a, b in rows]


def replace_cohort(symbol: str, holders: list[dict]) -> None:
    con = get_connection()
    try:
        con.execute("DELETE FROM holders WHERE token_symbol = ?", (symbol,))
        con.executemany(
            """INSERT INTO holders
               (token_symbol, address, rank, balance_at_pull, wallet_type, dune_balance, verified_block)
               VALUES (?,?,?,?,?,?,?)""",
            [(symbol, h["holder_address"], h["rank"], h["balance"], h["wallet_type"],
              h["dune_balance"], h["verified_block"]) for h in holders],
        )
        con.commit()
        print(f"  Wrote {len(holders)} holders to DB for {symbol}")
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
        print(f"  Loaded {len(raw_rows)} rows from the last saved Dune pull")
    else:
        raw_rows = fetch_top_holders_from_dune(symbol, token)
        save_raw_pull(symbol, pull_id, raw_rows)
    clean = apply_exclusion_filter(raw_rows, symbol, token)

    if not clean:
        print("  WARNING: no clean holders after filtering — broaden exclusion list or check Dune results")
        return

    upsert_token(symbol, token)
    replace_cohort(symbol, clean)

    print(f"\n  Top 10 clean holders:")
    for h in clean[:10]:
        print(f"    #{h['rank']:>3}  {h['holder_address']}  {h['balance']:>20,.4f} {symbol}  [{h['wallet_type']}]")


def main():
    parser = argparse.ArgumentParser(description="Fetch and filter top holders from Dune")
    parser.add_argument("--symbol", default=None, help="Single token symbol (e.g. LINK)")
    parser.add_argument("--from-raw", action="store_true",
                        help="Re-filter the last saved Dune pull instead of querying Dune")
    args = parser.parse_args()

    if not DUNE_API_KEY and not args.from_raw:
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
        if not args.from_raw:
            time.sleep(15)  # Dune free-tier rate limiting — conservative to avoid 429s


if __name__ == "__main__":
    main()
