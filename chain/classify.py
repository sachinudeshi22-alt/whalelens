"""
Wallet-type classification.

The old filter dropped anything with bytecode. That wrongly removed:
  - Gnosis Safe multisigs (how funds, DAOs and serious holders actually custody)
  - EOAs that set an EIP-7702 delegation (post-Pectra, their code is 0xef0100…)

Types returned:
  eoa      — no code
  eip7702  — EOA with a 7702 delegation designator (still a person-controlled key)
  safe     — contract that answers Safe.getThreshold() and getOwners()
  contract — anything else (pools, vaults, staking, bridges…) → excluded from cohort
"""
from chain.rpc import EIP7702_PREFIX, GET_OWNERS, GET_THRESHOLD, get_code, multicall

KEEP_TYPES = {"eoa", "eip7702", "safe"}


def _looks_like_safe(threshold: bytes | None, owners: bytes | None) -> bool:
    """Both calls must succeed: threshold ≥ 1 and a non-empty owner list."""
    if not threshold or not owners or len(threshold) != 32 or len(owners) < 64:
        return False
    # getOwners() returns a dynamic address[]: offset word + length word + entries
    n_owners = int.from_bytes(owners[32:64], "big")
    return int.from_bytes(threshold, "big") >= 1 and 1 <= n_owners <= 1000


def classify(addresses: list[str]) -> dict[str, str]:
    codes = get_code(addresses)
    types = {}
    probe = []
    for addr, code in codes.items():
        if code in ("0x", "0x0", ""):
            types[addr] = "eoa"
        elif code.startswith(EIP7702_PREFIX) and len(code) == 2 + 46:
            types[addr] = "eip7702"
        else:
            probe.append(addr)

    if probe:
        calls = []
        for addr in probe:
            calls += [(addr, GET_THRESHOLD), (addr, GET_OWNERS)]
        res = multicall(calls)   # reverts come back as None
        for i, addr in enumerate(probe):
            types[addr] = "safe" if _looks_like_safe(res[2 * i], res[2 * i + 1]) else "contract"
    return types
