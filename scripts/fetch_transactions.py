"""
Fetches normal ETH transactions and ERC-20 token transfers for all whale wallets
from Etherscan and stores results in SQLite.
Usage:
    python scripts/fetch_transactions.py
    python scripts/fetch_transactions.py --address 0xABC... --limit 10
"""
import argparse
import sys
import time
import requests
import sqlite3
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import ETHERSCAN_API_KEY, ETHERSCAN_BASE_URL, DB_PATH, WHALE_WALLETS
from db.schema import init_db, get_connection


def fetch_transactions(address: str, limit: int = 10) -> list[dict]:
    """Return the most recent `limit` normal transactions for `address`."""
    params = {
        "module":    "account",
        "action":    "txlist",
        "address":   address,
        "startblock": 0,
        "endblock":  99999999,
        "page":      1,
        "offset":    limit,
        "sort":      "desc",
        "apikey":    ETHERSCAN_API_KEY,
    }

    resp = requests.get(ETHERSCAN_BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data["status"] != "1":
        # status "0" with message "No transactions found" is not an error
        if "No transactions" in data.get("message", ""):
            return []
        raise ValueError(f"Etherscan error: {data.get('message')} — {data.get('result')}")

    return data["result"]


def store_transactions(address: str, txs: list[dict]) -> int:
    """Insert transactions into SQLite, skipping duplicates. Returns rows inserted."""
    con = get_connection()
    cur = con.cursor()

    inserted = 0
    for tx in txs:
        try:
            cur.execute(
                """
                INSERT OR IGNORE INTO transactions
                    (wallet_address, tx_hash, block_number, timestamp,
                     from_address, to_address, value_eth, gas_used, is_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address.lower(),
                    tx["hash"],
                    int(tx["blockNumber"]),
                    int(tx["timeStamp"]),
                    tx["from"].lower(),
                    tx["to"].lower() if tx["to"] else None,
                    int(tx["value"]) / 1e18,          # wei -> ETH
                    int(tx["gasUsed"]),
                    int(tx["isError"]),
                ),
            )
            if cur.rowcount:
                inserted += 1
        except sqlite3.Error as e:
            print(f"  DB error for tx {tx.get('hash')}: {e}")

    con.commit()
    con.close()
    return inserted


def fetch_token_transfers(address: str, limit: int = 10) -> list[dict]:
    """Return the most recent `limit` ERC-20 token transfers for `address`."""
    params = {
        "module":    "account",
        "action":    "tokentx",
        "address":   address,
        "startblock": 0,
        "endblock":  99999999,
        "page":      1,
        "offset":    limit,
        "sort":      "desc",
        "apikey":    ETHERSCAN_API_KEY,
    }

    resp = requests.get(ETHERSCAN_BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data["status"] != "1":
        if "No transactions" in data.get("message", ""):
            return []
        raise ValueError(f"Etherscan error: {data.get('message')} — {data.get('result')}")

    return data["result"]


def store_token_transfers(address: str, txs: list[dict]) -> int:
    """Insert ERC-20 token transfers into SQLite, skipping duplicates. Returns rows inserted."""
    con = get_connection()
    cur = con.cursor()

    inserted = 0
    for tx in txs:
        try:
            decimal = int(tx.get("tokenDecimal", 18) or 18)
            value_raw = tx.get("value", "0")
            value_decimal = int(value_raw) / (10 ** decimal) if decimal > 0 else float(value_raw)

            cur.execute(
                """
                INSERT OR IGNORE INTO token_transfers
                    (wallet_address, tx_hash, block_number, timestamp,
                     from_address, to_address, contract_address,
                     token_name, token_symbol, token_decimal,
                     value_raw, value_decimal, gas_used)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address.lower(),
                    tx["hash"],
                    int(tx["blockNumber"]),
                    int(tx["timeStamp"]),
                    tx["from"].lower(),
                    tx["to"].lower() if tx["to"] else None,
                    tx.get("contractAddress", "").lower(),
                    tx.get("tokenName", ""),
                    tx.get("tokenSymbol", ""),
                    decimal,
                    value_raw,
                    value_decimal,
                    int(tx.get("gasUsed", 0)),
                ),
            )
            if cur.rowcount:
                inserted += 1
        except sqlite3.Error as e:
            print(f"  DB error for token tx {tx.get('hash')}: {e}")

    con.commit()
    con.close()
    return inserted


def print_token_summary(address: str, txs: list[dict]):
    print(f"\n  --- ERC-20 transfers ({len(txs)}) ---")
    for tx in txs:
        decimal = int(tx.get("tokenDecimal", 18) or 18)
        value_raw = tx.get("value", "0")
        amount = int(value_raw) / (10 ** decimal) if decimal > 0 else float(value_raw)
        direction = "OUT" if tx["from"].lower() == address.lower() else "IN "
        symbol = tx.get("tokenSymbol", "???")
        print(
            f"  {direction} | {amount:>18.4f} {symbol:<8} | "
            f"block {tx['blockNumber']} | {tx['hash'][:18]}..."
        )


def print_summary(address: str, txs: list[dict]):
    print(f"\n{'='*60}")
    print(f"Wallet : {address}")
    print(f"Fetched: {len(txs)} transactions")
    print(f"{'='*60}")
    for tx in txs:
        eth_val = int(tx["value"]) / 1e18
        direction = "OUT" if tx["from"].lower() == address.lower() else "IN "
        print(
            f"  {direction} | {eth_val:>12.6f} ETH | "
            f"block {tx['blockNumber']} | {tx['hash'][:18]}..."
        )


def main():
    parser = argparse.ArgumentParser(description="Fetch whale wallet transactions from Etherscan")
    parser.add_argument("--address", default=None, help="Single wallet address to fetch")
    parser.add_argument("--limit",   type=int, default=10, help="Number of transactions to fetch")
    args = parser.parse_args()

    if not ETHERSCAN_API_KEY:
        sys.exit("ERROR: ETHERSCAN_API_KEY not set. Copy .env.example to .env and add your key.")

    init_db()

    if args.address:
        wallets = {"custom": args.address}
    else:
        wallets = WHALE_WALLETS

    for label, address in wallets.items():
        print(f"\nFetching data for: {label} ({address})")
        try:
            txs = fetch_transactions(address, limit=args.limit)
            print_summary(address, txs)
            inserted_eth = store_transactions(address, txs)

            time.sleep(0.25)

            token_txs = fetch_token_transfers(address, limit=args.limit)
            print_token_summary(address, token_txs)
            inserted_tokens = store_token_transfers(address, token_txs)

            print(f"\n  Stored: {inserted_eth} ETH tx(s), {inserted_tokens} token transfer(s)")
        except ValueError as e:
            print(f"  Skipping — {e}")
        except requests.RequestException as e:
            print(f"  Network error — {e}")

        time.sleep(0.25)   # rate limit between wallets


if __name__ == "__main__":
    main()
