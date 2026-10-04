"""
Minimal Ethereum JSON-RPC client: batched calls, retries, endpoint fallback.

Unlike the old inline RPC code, a batch that still fails after every endpoint
has been tried raises RpcError — it never silently skips addresses, because a
skipped contract check lets contracts leak into the cohort.
"""
import time
import requests
from eth_abi import decode, encode

from config import ETH_RPC_ENDPOINTS

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
                            "stackunderflow", "out of gas", "gas required exceeds", "execution")


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


class RateLimited(RpcError):
    pass


def _is_rate_limit(err) -> bool:
    msg = str(err.get("message", "") if isinstance(err, dict) else err).lower()
    code = err.get("code") if isinstance(err, dict) else None
    return code == 429 or "compute units per second" in msg or "rate limit" in msg or "too many requests" in msg


def _post_paced(url: str, payload: list[dict], attempts: int = 6) -> list[dict]:
    """POST, waiting out per-second throughput limits on the same endpoint before giving up."""
    for attempt in range(attempts):
        try:
            body = _post(url, payload)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise
        if any(isinstance(i, dict) and "error" in i and _is_rate_limit(i["error"]) for i in body):
            time.sleep(0.5 * 2 ** attempt)
            continue
        return body
    raise RateLimited(f"{_host(url)} still rate-limited after {attempts} attempts")


def _run_chunk(url: str, max_batch: int, chunk: list[tuple[str, list]]) -> list:
    """Run `chunk` on one endpoint, splitting into that endpoint's batch size."""
    out = []
    for sub_start in range(0, len(chunk), max_batch):
        sub = chunk[sub_start:sub_start + max_batch]
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(sub)]
        by_id = {item["id"]: item for item in _post_paced(url, payload) if isinstance(item, dict)}
        if len(by_id) != len(sub):
            raise RpcError(f"{_host(url)} returned {len(by_id)}/{len(sub)} items")
        for i in range(len(sub)):
            item = by_id[i]
            if "error" in item:
                if sub[i][0] == "eth_call" and _is_revert(item["error"]):
                    out.append(Reverted(item["error"].get("message")))
                    continue
                raise RpcError(f"{_host(url)}: {item['error']}")
            out.append(item.get("result"))
    return out


def _host(url: str) -> str:
    # Never log full URLs: keyed endpoints embed the API key in the path
    return url.split("/")[2] if "://" in url else url


def batch(calls: list[tuple[str, list]], retries: int = 8) -> list:
    """
    Run [(method, params), ...] and return results in the same order.
    An eth_call that reverts comes back as a `Reverted` instance instead of raising.
    Any other per-item error retries the chunk on the next endpoint.
    """
    results: list = []
    chunk_size = max(size for _, size in ETH_RPC_ENDPOINTS)
    for start in range(0, len(calls), chunk_size):
        chunk = calls[start:start + chunk_size]
        last_err = None
        done = False
        for attempt in range(retries):
            for url, max_batch in ETH_RPC_ENDPOINTS:
                try:
                    results += _run_chunk(url, max_batch, chunk)
                    done = True
                    break
                except (requests.RequestException, RpcError, ValueError, KeyError) as e:
                    last_err = RpcError(f"{_host(url)}: {type(e).__name__}: {str(e).replace(url, _host(url))[:200]}")
            if done:
                break
            # Back off up to a minute per round (~4 min total), so a brief network or
            # DNS outage pauses a long job instead of killing it
            time.sleep(min(2 ** attempt, 60))
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


# Multicall3: one eth_call runs many calls, so throughput limits (compute units per
# call) stop mattering for bulk reads. Deployed at the same address on most chains.
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
MULTICALL3_DEPLOY_BLOCK = 14353601
_AGGREGATE3 = "0x82ad56cb"   # aggregate3((address,bool,bytes)[]) -> (bool,bytes)[]
_MULTICALL_CHUNK = 300
_MULTICALLS_PER_REQUEST = 4


def _individually(calls: list[tuple[str, str]], tag: str) -> list[bytes | None]:
    """Fallback: plain eth_calls, each with its own gas cap; reverts become None."""
    res = batch([("eth_call", [{"to": to, "data": cd}, tag]) for to, cd in calls])
    return [None if isinstance(r, Reverted) or r is None else bytes.fromhex(r[2:]) for r in res]


def multicall(calls: list[tuple[str, str]], block="latest") -> list[bytes | None]:
    """
    Run [(to, calldata_hex), ...] through Multicall3 at `block`.
    Returns raw return bytes per call, or None where that call reverted.

    A sub-call that burns all its gas (e.g. probing an odd contract for a Safe
    method) sinks the whole aggregate call, since Multicall3 can't cap gas per
    call; such chunks are retried call by call.
    """
    tag = _block_tag(block)
    chunks = [calls[i:i + _MULTICALL_CHUNK] for i in range(0, len(calls), _MULTICALL_CHUNK)]
    rpc_calls = [
        ("eth_call", [{"to": MULTICALL3, "data": _AGGREGATE3 + encode(
            ["(address,bool,bytes)[]"], [[(to, True, bytes.fromhex(cd[2:])) for to, cd in chunk]]).hex()}, tag])
        for chunk in chunks
    ]
    # Each aggregate3 call is ~100KB of hex; keep JSON-RPC requests well under provider body limits
    results = []
    for g in range(0, len(rpc_calls), _MULTICALLS_PER_REQUEST):
        results += batch(rpc_calls[g:g + _MULTICALLS_PER_REQUEST])
    out: list[bytes | None] = []
    for chunk, r in zip(chunks, results):
        if isinstance(r, Reverted):
            out += _individually(chunk, tag)
            continue
        (decoded,) = decode(["(bool,bytes)[]"], bytes.fromhex(r[2:]))
        out += [ret if ok else None for ok, ret in decoded]
    return out


def _supports_multicall(block) -> bool:
    return isinstance(block, str) or block >= MULTICALL3_DEPLOY_BLOCK


def balances_of(token: str, addresses: list[str], block="latest") -> dict[str, int]:
    """Raw (undivided) ERC-20 balances at `block`."""
    datas = [BALANCE_OF + a.lower().replace("0x", "").rjust(64, "0") for a in addresses]
    out = {}
    if _supports_multicall(block):
        for a, ret in zip(addresses, multicall([(token, d) for d in datas], block)):
            if ret is None:
                raise RpcError(f"balanceOf reverted for {a} on {token}")
            out[a.lower()] = int.from_bytes(ret[:32], "big") if ret else 0
        return out
    calls = [("eth_call", [{"to": token, "data": d}, _block_tag(block)]) for d in datas]
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
