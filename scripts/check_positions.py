"""
Coverage check for staking position sources: tracked participants' positions
should add up to (nearly) everything the escrow contract holds.

Usage:
    python scripts/check_positions.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TOKEN_BASKET
from db.schema import init_db
from chain.positions import POSITION_SOURCES, LockstakePosition, VeBALPosition, BALANCE_OF, _uint, _word
from chain.rpc import multicall


def contract_total(symbol: str, source) -> float:
    """What the escrow holds, in token units."""
    token = TOKEN_BASKET[symbol]
    if isinstance(source, LockstakePosition):
        return None
    if isinstance(source, VeBALPosition):
        bpt = _uint(multicall([(source.asset, BALANCE_OF + _word(source.contract))])[0])
        return bpt * source.bal_per_bpt("latest") / 10 ** token["decimals"]
    held = _uint(multicall([(source.asset, BALANCE_OF + _word(source.contract))])[0])
    return held / 10 ** token["decimals"]


def main():
    init_db()
    for symbol, sources in POSITION_SOURCES.items():
        token = TOKEN_BASKET[symbol]
        for src in sources:
            pos = src.all_positions("latest")
            tracked = sum(pos.values()) / 10 ** token["decimals"]
            total = contract_total(symbol, src)
            n = sum(1 for v in pos.values() if v > 0)
            cov = f"{tracked / total:6.1%} of {total:,.0f} held" if total else "n/a"
            print(f"{symbol:<6} {src.name:<13} {n:>6,} positions  tracked {tracked:>16,.0f}  coverage {cov}")


if __name__ == "__main__":
    main()
