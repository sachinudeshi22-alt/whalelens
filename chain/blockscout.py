"""
Blockscout client: holder lists, logs, and token transfers, with rate-limit backoff.

The free tier rate-limits aggressively ("Too many requests"). Setting
BLOCKSCOUT_API_KEY in .env raises the limit; without it we back off and retry.
"""
import time
import requests

from config import BLOCKSCOUT_BASE_URL, BLOCKSCOUT_API_KEY

class BlockscoutError(RuntimeError):
    pass


# Keyless limit is 10 requests per window, and bursting past it earns a multi-minute
# lockout, so stay well under it rather than relying on retries.
_MIN_INTERVAL = 0.35 if not BLOCKSCOUT_API_KEY else 0.1
_MAX_LOCKOUT_WAIT = 900
_last_request = 0.0


def _throttle():
    global _last_request
    wait = _MIN_INTERVAL - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()


def _reset_seconds(resp: requests.Response) -> float:
    try:
        return int(resp.headers.get("x-ratelimit-reset", "0")) / 1000   # header is in ms
    except ValueError:
        return 0


def _get(url: str, params: dict | None = None, retries: int = 6) -> dict:
    params = dict(params or {})
    if BLOCKSCOUT_API_KEY:
        params["apikey"] = BLOCKSCOUT_API_KEY
    last = None
    for attempt in range(retries):
        _throttle()
        try:
            resp = requests.get(url, params=params, timeout=60)
            body = resp.json() if resp.content else {}
            limited = resp.status_code == 429 or (
                isinstance(body, dict) and "too many requests" in str(body.get("message", "")).lower())
            if limited:
                wait = min(max(_reset_seconds(resp), 2 ** attempt), _MAX_LOCKOUT_WAIT)
                print(f"    Blockscout rate limit — waiting {wait:.0f}s")
                time.sleep(wait + 1)
                last = BlockscoutError("rate limited")
                continue
            resp.raise_for_status()
            if resp.headers.get("x-ratelimit-remaining") == "0":
                time.sleep(_reset_seconds(resp))
            return body
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(min(2 ** attempt, 30))
    raise BlockscoutError(f"GET {url} failed after {retries} attempts: {last}")


def address_labels(address: dict) -> list[str]:
    """Flatten Blockscout's metadata tags, public tags and contract name into one list."""
    labels = [t.get("name") for t in ((address.get("metadata") or {}).get("tags") or [])]
    labels += [t.get("display_name") for t in address.get("public_tags") or []]
    if address.get("name"):
        labels.append(address["name"])   # contract name, e.g. "GnosisSafeProxy"
    return [l for l in labels if l]


def token_holders(contract: str, limit: int) -> list[dict]:
    """
    Top holders by current wallet balance (50/page).
    Each row: {address: str, value: int (raw), labels: list[str]}
    """
    url = f"{BLOCKSCOUT_BASE_URL}/tokens/{contract}/holders"
    rows, params = [], {}
    while len(rows) < limit:
        data = _get(url, params)
        for item in data["items"]:
            rows.append({
                "address": item["address"]["hash"].lower(),
                "value": int(item["value"]),
                "labels": address_labels(item["address"]),
            })
        params = data.get("next_page_params")
        if not params:
            break
        time.sleep(0.5)
    return rows[:limit]


def get_logs(address: str, topic0: str, from_block: int = 0) -> list[dict]:
    """
    Logs emitted by `address` with `topic0`, at or after `from_block`.

    Uses the v2 API (newest first) and stops paging once it passes `from_block`,
    so incremental syncs are cheap. The keyless legacy /api endpoint that could
    filter by topic server-side locks clients out for 15 minutes at a time.
    Returned logs use the Etherscan-style shape: {topics, data, blockNumber (hex), ...}.
    """
    url = f"{BLOCKSCOUT_BASE_URL}/addresses/{address}/logs"
    out, params = [], {}
    while True:
        data = _get(url, params)
        items = data.get("items") or []
        for log in items:
            if log["block_number"] < from_block:
                return out
            topics = [t for t in log["topics"] if t]
            if topics and topics[0].lower() == topic0.lower():
                out.append({"topics": topics, "data": log["data"],
                            "blockNumber": hex(log["block_number"]),
                            "transactionHash": log["transaction_hash"], "logIndex": log["index"]})
        params = data.get("next_page_params")
        if not params:
            return out


class TooManyTransfers(Exception):
    """The address has more transfers than we page through; its first funder is unknown."""


def first_inflows(token: str, address: str, n: int = 1, max_pages: int = 10) -> list[dict]:
    """
    Oldest `n` inbound transfers of `token` to `address`: [{from, value, block, timestamp}].
    The v2 API lists newest first, so this pages to the end; raises TooManyTransfers
    past `max_pages` (50 transfers/page) rather than guessing.
    """
    url = f"{BLOCKSCOUT_BASE_URL}/addresses/{address}/token-transfers"
    base = {"type": "ERC-20", "filter": "to", "token": token}
    items, params = [], dict(base)
    for _ in range(max_pages):
        data = _get(url, params)
        items += data.get("items") or []
        nxt = data.get("next_page_params")
        if not nxt:
            oldest = list(reversed(items))[:n]
            return [{"from": t["from"]["hash"].lower(), "value": int(t["total"]["value"]),
                     "block": t["block_number"], "timestamp": t["timestamp"]} for t in oldest]
        params = {**base, **nxt}
    raise TooManyTransfers(address)


def address_info(address: str) -> dict:
    """{is_contract, labels} for one address."""
    data = _get(f"{BLOCKSCOUT_BASE_URL}/addresses/{address}")
    return {"is_contract": bool(data.get("is_contract")), "labels": address_labels(data)}
