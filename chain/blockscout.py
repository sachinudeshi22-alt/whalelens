"""
Blockscout client: holder lists, logs, and token transfers, with rate-limit backoff.

With BLOCKSCOUT_API_KEY set, requests go to the PRO API (5 req/s, metered in
credits) until credits fall below BLOCKSCOUT_CREDIT_RESERVE, then fall back to
the free endpoint. The free endpoint rate-limits aggressively and locks out
bursts, so it is throttled harder.
"""
import time
import requests

from config import (
    BLOCKSCOUT_API_KEY, BLOCKSCOUT_BASE_URL, BLOCKSCOUT_CREDIT_RESERVE, BLOCKSCOUT_PRO_URL,
)

class BlockscoutError(RuntimeError):
    pass


# Free endpoint: 10 requests per window, and bursting past it earns a multi-minute
# lockout, so stay well under it. PRO: 5 requests per second.
_FREE_INTERVAL = 0.35
_PRO_INTERVAL = 0.22
_MAX_LOCKOUT_WAIT = 900
_last_request = 0.0
_use_pro = bool(BLOCKSCOUT_API_KEY)
credits_remaining: int | None = None


def _base() -> str:
    return BLOCKSCOUT_PRO_URL if _use_pro else BLOCKSCOUT_BASE_URL


def _throttle(pro: bool):
    global _last_request
    wait = (_PRO_INTERVAL if pro else _FREE_INTERVAL) - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()


def _track_credits(resp: requests.Response) -> None:
    global credits_remaining, _use_pro
    raw = resp.headers.get("x-credits-remaining")
    if raw is None:
        return
    credits_remaining = int(raw)
    if _use_pro and credits_remaining < BLOCKSCOUT_CREDIT_RESERVE:
        _use_pro = False
        print(f"    Blockscout PRO credits at {credits_remaining:,} — switching to the free endpoint")


def _reset_seconds(resp: requests.Response) -> float:
    try:
        return int(resp.headers.get("x-ratelimit-reset", "0")) / 1000   # header is in ms
    except ValueError:
        return 0


def _get(url: str, params: dict | None = None, retries: int = 6) -> dict:
    params = dict(params or {})
    pro = url.startswith(BLOCKSCOUT_PRO_URL)
    if pro:
        params["apikey"] = BLOCKSCOUT_API_KEY
    last = None
    for attempt in range(retries):
        _throttle(pro)
        try:
            resp = requests.get(url, params=params, timeout=60)
            if pro:
                _track_credits(resp)
            body = resp.json() if resp.content else {}
            limited = resp.status_code == 429 or (
                isinstance(body, dict) and "too many requests" in str(body.get("message", "")).lower())
            if limited:
                wait = min(max(_reset_seconds(resp), 2 ** attempt), _MAX_LOCKOUT_WAIT)
                print(f"    Blockscout rate limit — waiting {wait:.0f}s")
                time.sleep(wait + 1)
                last = BlockscoutError("rate limited")
                continue
            if pro and resp.status_code in (401, 402):
                raise BlockscoutError(f"PRO API refused the key ({resp.status_code})")
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
    url = f"{_base()}/tokens/{contract}/holders"
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
    url = f"{_base()}/addresses/{address}/logs"
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
    url = f"{_base()}/addresses/{address}/token-transfers"
    base = {"type": "ERC-20", "filter": "to", "token": token}
    items, params = [], dict(base)
    for _ in range(max_pages):
        data = _get(url, params)
        items += data.get("items") or []
        nxt = data.get("next_page_params")
        if not nxt:
            oldest = list(reversed(items))[:n]
            return [{"from": t["from"]["hash"].lower(), "from_labels": address_labels(t["from"]),
                     "value": int(t["total"]["value"]),
                     "block": t["block_number"], "timestamp": t["timestamp"]} for t in oldest]
        params = {**base, **nxt}
    raise TooManyTransfers(address)


METADATA_URL = "https://metadata.services.blockscout.com/api/v1/metadata"


def metadata_labels(addresses: list[str], chain_id: int = 1) -> dict[str, list[str]]:
    """
    Public tag names per address from Blockscout's metadata service (the source of the
    labels embedded in holder/transfer lists). The single-address endpoint omits them.
    """
    out = {a.lower(): [] for a in addresses}
    for i in range(0, len(addresses), 50):
        chunk = addresses[i:i + 50]
        data = _get(METADATA_URL, {"addresses": ",".join(chunk), "chainId": chain_id})
        for addr, entry in (data.get("addresses") or {}).items():
            out[addr.lower()] = [t["name"] for t in entry.get("tags", []) if t.get("name")]
    return out


def address_info(address: str) -> dict:
    """{is_contract, labels} for one address — labels include public metadata tags."""
    data = _get(f"{_base()}/addresses/{address}")
    labels = address_labels(data) + metadata_labels([address])[address.lower()]
    return {"is_contract": bool(data.get("is_contract")), "labels": list(dict.fromkeys(labels))}
