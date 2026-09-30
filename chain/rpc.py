"""
Minimal Ethereum JSON-RPC client: batched calls, retries, endpoint fallback.

Unlike the old inline RPC code, a batch that still fails after every endpoint
has been tried raises RpcError — it never silently skips addresses, because a
skipped contract check lets contracts leak into the cohort.
"""
import time
import requests

from config import ETH_RPC_URLS

BALANCE_OF   = "0x70a08231"   # balanceOf(address)
GET_THRESHOLD = "0xe75235b8"  # Safe.getThreshold()
GET_OWNERS    = "0xa0e67e2b"  # Safe.getOwners()

# EIP-7702 delegation designator: 0xef0100 || 20-byte delegate address
EIP7702_PREFIX = "0xef0100"


class RpcError(RuntimeError):
    pass


class Reverted(Exception):
    """eth_call reverted — a normal outcome (e.g. probing a non-Safe for getThreshold)."""


# Execution failures inside the EVM (as opposed to transport / rate-limit errors).
# Providers word these differently, e.g. blastapi: "EVM error: StackUnderflow".
_EXECUTION_ERROR_MARKERS = ("revert", "evm error", "invalid opcode", "stack underflow",
                            "stackunderflow", "out of gas", "execution")


def _is_revert(err: dict) -> bool:
    msg = str(err.get("message", "")).lower()
    return err.get("code") == 3 or any(m in msg for m in _EXECUTION_ERROR_MARKERS)


def _post(url: str, payload: list[dict]) -> list[dict]:
    resp = requests.post(url, json=payload, timeout=45)
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, list):
        # Some providers answer a whole batch with a single error object
        raise RpcError(f"non-batch response: {str(body)[:200]}")
    return body


def batch(calls: list[tuple[str, list]], batch_size: int = 25, retries: int = 3) -> list:
    """
    Run [(method, params), ...] and return results in the same order.
    An eth_call that reverts comes back as a `Reverted` instance instead of raising.
    Any other per-item error triggers a retry of the whole sub-batch.
    """
    results: list = [None] * len(calls)
    for start in range(0, len(calls), batch_size):
        chunk = calls[start:start + batch_size]
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": m, "params": p}
            for i, (m, p) in enumerate(chunk)
        ]
        last_err = None
        done = False
        for attempt in range(retries):
            for url in ETH_RPC_URLS:
                try:
                    by_id = {item["id"]: item for item in _post(url, payload) if isinstance(item, dict)}
                    if len(by_id) != len(chunk):
                        raise RpcError(f"{url} returned {len(by_id)}/{len(chunk)} items")
                    out = []
                    for i in range(len(chunk)):
                        item = by_id[i]
                        if "error" in item:
                            if chunk[i][0] == "eth_call" and _is_revert(item["error"]):
                                out.append(Reverted(item["error"].get("message")))
                                continue
                            raise RpcError(f"{url}: {item['error']}")
                        out.append(item.get("result"))
                    results[start:start + len(chunk)] = out
                    done = True
                    break
                except (requests.RequestException, RpcError, ValueError, KeyError) as e:
                    last_err = e
            if done:
                break
            time.sleep(2 ** attempt)
        if not done:
            raise RpcError(f"batch at offset {start} failed on all endpoints: {last_err}")
    return results


def _block_tag(block) -> str:
    return block if isinstance(block, str) else hex(block)


def latest_block() -> int:
    return int(batch([("eth_blockNumber", [])])[0], 16)


def get_code(addresses: list[str], block="latest") -> dict[str, str]:
    res = batch([("eth_getCode", [a, _block_tag(block)]) for a in addresses])
    return {a.lower(): (code or "0x").lower() for a, code in zip(addresses, res)}


def balances_of(token: str, addresses: list[str], block="latest") -> dict[str, int]:
    """Raw (undivided) ERC-20 balances at `block`."""
    calls = [
        ("eth_call", [{"to": token, "data": BALANCE_OF + a.lower().replace("0x", "").rjust(64, "0")},
                      _block_tag(block)])
        for a in addresses
    ]
    out = {}
    for a, r in zip(addresses, batch(calls)):
        if isinstance(r, Reverted):
            raise RpcError(f"balanceOf reverted for {a} on {token}")
        out[a.lower()] = int(r, 16) if r and r != "0x" else 0
    return out


def block_at_timestamp(ts: int) -> int:
    """Last block with timestamp <= ts (binary search, ~20 calls)."""
    def block_ts(n: int) -> int:
        return int(batch([("eth_getBlockByNumber", [hex(n), False])])[0]["timestamp"], 16)

    hi = latest_block()
    if block_ts(hi) <= ts:
        return hi
    # Post-merge blocks are ~12s apart; start with a narrow window around the estimate
    est = hi - (block_ts(hi) - ts) // 12
    lo = max(0, est - 2000)
    while block_ts(lo) > ts:
        lo = max(0, lo - 20000)
    hi = min(hi, est + 2000)
    while block_ts(hi) <= ts:
        hi += 20000
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if block_ts(mid) <= ts:
            lo = mid
        else:
            hi = mid
    return lo
