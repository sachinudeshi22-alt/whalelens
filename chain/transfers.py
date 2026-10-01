"""
Per-address transfer aggregates from alchemy_getAssetTransfers.

Used to build point-in-time cohorts: an address whose holdings changed during a
period must appear in that period's transfers, so scanning them finds former
whales that today's holder list no longer shows. We keep per-address totals
(sent, received, transfer counts) rather than individual transfers.

Requires ETH_RPC_URL to be an Alchemy endpoint (getLogs on free tiers is capped
at 10-block ranges, so it can't be used for this).
"""
import time
import requests

from config import ETH_RPC_URL
from db.schema import get_connection

ZERO = "0x" + "0" * 40


class TransfersUnavailable(RuntimeError):
    pass


def _call(params: dict, attempts: int = 15) -> dict:
    if "alchemy.com" not in ETH_RPC_URL:
        raise TransfersUnavailable("ETH_RPC_URL must be an Alchemy endpoint for transfer scans")
    for attempt in range(attempts):
        try:
            resp = requests.post(ETH_RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                                                    "method": "alchemy_getAssetTransfers",
                                                    "params": [params]}, timeout=90)
            if resp.status_code == 429:
                time.sleep(0.5 * 2 ** attempt)
                continue
            resp.raise_for_status()
            body = resp.json()
            err = body.get("error")
            if err:
                if err.get("code") == 429 or "compute units" in str(err.get("message", "")).lower():
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                raise TransfersUnavailable(f"alchemy_getAssetTransfers: {err}")
            return body["result"]
        except (requests.RequestException, ValueError):
            time.sleep(min(2 ** attempt, 60))
    raise TransfersUnavailable("alchemy_getAssetTransfers kept failing")


_CHUNK_BLOCKS = 50_000   # ~1 week; progress is checkpointed after each chunk


def scan(symbol: str, contract: str, decimals: int, from_block: int, to_block: int) -> int:
    """
    Aggregate transfers of `contract` in [from_block, to_block] into transfer_totals.
    Works in ~weekly block chunks and checkpoints scan_state after each, so an
    interrupted scan resumes without double counting. Returns transfers processed.
    """
    con = get_connection()
    row = con.execute("SELECT next_block FROM scan_state WHERE token_symbol = ?", (symbol,)).fetchone()
    start = max(from_block, row[0]) if row else from_block
    scale = 10 ** decimals
    processed = 0

    while start <= to_block:
        end = min(start + _CHUNK_BLOCKS - 1, to_block)
        totals: dict[str, list] = {}   # addr -> [sent, received, n_out, n_in]
        params = {"fromBlock": hex(start), "toBlock": hex(end), "contractAddresses": [contract],
                  "category": ["erc20"], "maxCount": hex(1000), "withMetadata": False,
                  "excludeZeroValue": True, "order": "asc"}
        while True:
            res = _call(params)
            for t in res.get("transfers", []):
                raw = t.get("rawContract", {}).get("value")
                value = int(raw, 16) / scale if raw else float(t.get("value") or 0)
                frm, to = (t.get("from") or ZERO).lower(), (t.get("to") or ZERO).lower()
                snd = totals.setdefault(frm, [0.0, 0.0, 0, 0]); snd[0] += value; snd[2] += 1
                rcv = totals.setdefault(to, [0.0, 0.0, 0, 0]); rcv[1] += value; rcv[3] += 1
                processed += 1
            key = res.get("pageKey")
            if not key:
                break
            params["pageKey"] = key
        con.executemany(
            """INSERT INTO transfer_totals (token_symbol, address, sent, received, n_out, n_in)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(token_symbol, address) DO UPDATE SET
                 sent = sent + excluded.sent, received = received + excluded.received,
                 n_out = n_out + excluded.n_out, n_in = n_in + excluded.n_in""",
            [(symbol, a, v[0], v[1], v[2], v[3]) for a, v in totals.items()],
        )
        con.execute("INSERT OR REPLACE INTO scan_state (token_symbol, from_block, next_block) "
                    "VALUES (?, COALESCE((SELECT from_block FROM scan_state WHERE token_symbol = ?), ?), ?)",
                    (symbol, symbol, from_block, end + 1))
        con.commit()
        print(f"    {symbol}: through block {end:,} — {processed:,} transfers")
        start = end + 1
    con.close()
    return processed
