"""
Insider classification and Safe entity grouping.

Rules are listed in config.py next to INSIDER_LABEL_KEYWORDS. Every insider gets
a human-readable `insider_reason` so the public methodology can show why.
"""
import re
from collections import Counter

from config import INSIDER_LABEL_KEYWORDS, INSIDER_SOURCES, NEUTRAL_FUNDERS
from chain.blockscout import TooManyTransfers, address_info, first_inflows
from chain.rpc import GET_OWNERS, Reverted, batch

_INSIDER_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in INSIDER_LABEL_KEYWORDS) + r")\b", re.I)
_funder_cache: dict[str, dict] = {}


def insider_label(labels: list[str]) -> str | None:
    for label in labels:
        if _INSIDER_RE.search(label):
            return label
    return None


def _funder_info(addr: str) -> dict:
    if addr not in _funder_cache:
        _funder_cache[addr] = address_info(addr)
    return _funder_cache[addr]


def _short(addr: str) -> str:
    return addr[:10] + "…"


def trace_funding(symbol: str, token: dict, h: dict) -> None:
    """Set h['first_funder'] and, if rules 1–2 match, h['insider_reason']."""
    own = insider_label(h.get("labels") or [])
    if own:
        h["insider_reason"] = f"labelled '{own}'"
    try:
        inflows = first_inflows(token["contract"], h["holder_address"], n=1)
    except TooManyTransfers:
        h["first_funder"] = None
        h["funding_note"] = "too many transfers to trace"
        return
    if not inflows:
        return
    funder = inflows[0]["from"]
    h["first_funder"] = funder
    if h.get("insider_reason"):
        return
    if funder in INSIDER_SOURCES.get(symbol, set()):
        h["insider_reason"] = f"first {symbol} received from project allocation contract {_short(funder)}"
        return
    if funder == "0x" + "0" * 40:
        return   # minted directly — not insider evidence on its own (e.g. SKY conversions)
    label = insider_label(_funder_info(funder)["labels"])
    if label:
        h["insider_reason"] = f"first {symbol} received from '{label}' ({_short(funder)})"


def safe_owners(addresses: list[str]) -> dict[str, set[str]]:
    res = batch([("eth_call", [{"to": a, "data": GET_OWNERS}, "latest"]) for a in addresses])
    out = {}
    for a, r in zip(addresses, res):
        if isinstance(r, Reverted) or not r or len(r) < 130:
            out[a] = set()
            continue
        body = r[2:]
        n = int(body[64:128], 16)
        out[a] = {"0x" + body[128 + 64 * i + 24:128 + 64 * (i + 1)] for i in range(n)}
    return out


def group_safes(holders: list[dict]) -> None:
    """
    Union Safes sharing at least half the signers of the smaller signer set, and
    set h['entity_id'] (the lowest address in the group) on every grouped Safe.
    Insider status spreads across a group (rule 3).
    """
    safes = [h for h in holders if h.get("wallet_type") == "safe"]
    if len(safes) < 2:
        return
    owners = safe_owners([h["holder_address"] for h in safes])
    parent = {h["holder_address"]: h["holder_address"] for h in safes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    addrs = list(parent)
    for i, a in enumerate(addrs):
        for b in addrs[i + 1:]:
            oa, ob = owners[a], owners[b]
            if oa and ob and len(oa & ob) * 2 >= min(len(oa), len(ob)):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)

    groups: dict[str, list[dict]] = {}
    for h in safes:
        groups.setdefault(find(h["holder_address"]), []).append(h)
    for root, members in groups.items():
        if len(members) < 2:
            continue
        for h in members:
            h["entity_id"] = root
        insider = next((m for m in members if m.get("insider_reason")), None)
        if insider:
            for h in members:
                if not h.get("insider_reason"):
                    h["insider_reason"] = f"Safe shares signers with insider {_short(insider['holder_address'])}"


def suggest_insider_sources(symbol: str, holders: list[dict]) -> list[tuple[str, int]]:
    """Funders that seeded 3+ holders and aren't already known — candidates for manual review."""
    known = INSIDER_SOURCES.get(symbol, set()) | NEUTRAL_FUNDERS
    counts = Counter(h["first_funder"] for h in holders
                     if h.get("first_funder") and h["first_funder"] not in known
                     and h["first_funder"] != "0x" + "0" * 40)
    return [(f, n) for f, n in counts.most_common() if n >= 3]
