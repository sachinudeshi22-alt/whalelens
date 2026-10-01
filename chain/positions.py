"""
Token held outside the wallet but still controlled by the holder.

A plain balanceOf() misses tokens a holder has staked, so staking looks like
selling. Each position source enumerates its participants and reads their
position size, and holdings() adds positions to wallet balances.

Only sources with a verified per-owner read are listed here. Current coverage:

  SKY — LockstakeEngine. Each owner opens urns (Open events); the SKY locked in
        an urn is its `ink` in the Vat under the engine's ilk. When an urn picks a
        vote delegate, its SKY physically moves to a VoteDelegate and on into the
        Chief, but the urn's ink still records it — so we count ink only and never
        count Chief/VoteDelegate balances again. (As of 2026-09-30, 99.999% of the
        SKY in Chief arrived via lockstake; direct delegation is ~40k SKY and ignored.)
"""
import json

from chain.blockscout import get_logs
from chain.rpc import balances_of, multicall
from db.schema import get_connection


def _word(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


class LockstakePosition:
    name = "lockstake"

    ENGINE = "0xce01c90de7fd1bcfa39e237fe6d8d9f569e8a6a3"
    VAT = "0x35d1b3f3d7966a1dfe207aa4514c12a259a0492b"
    ILK = "0x4c534556322d534b592d41000000000000000000000000000000000000000000"  # "LSEV2-SKY-A"
    OPEN_TOPIC = "0xdde6dd354074cad07a2dacbb612a6d2bac55ac537264d73250bf5c76bc15d64d"  # Open(address,uint256,address)
    URNS_SELECTOR = "0x2424be5c"   # Vat.urns(bytes32,address) -> (ink, art)
    DEPLOY_BLOCK = 22434171        # first Open event

    def _sync_urns(self) -> dict[str, list[str]]:
        """Incrementally cache (owner, urn) pairs from Open events; return owner -> urns."""
        con = get_connection()
        row = con.execute("SELECT MAX(block_number) FROM position_urns WHERE source = ?",
                          (self.name,)).fetchone()
        from_block = (row[0] or self.DEPLOY_BLOCK)
        logs = get_logs(self.ENGINE, self.OPEN_TOPIC, from_block)
        con.executemany(
            "INSERT OR IGNORE INTO position_urns (source, owner, idx, urn, block_number) VALUES (?,?,?,?,?)",
            [(self.name, "0x" + l["topics"][1][-40:], int(l["topics"][2], 16),
              "0x" + l["data"][-40:], int(l["blockNumber"], 16)) for l in logs],
        )
        con.commit()
        urns: dict[str, list[str]] = {}
        for owner, urn in con.execute("SELECT owner, urn FROM position_urns WHERE source = ?", (self.name,)):
            urns.setdefault(owner, []).append(urn)
        con.close()
        return urns

    def all_positions(self, block="latest") -> dict[str, int]:
        """Raw staked amount for every owner that ever opened an urn."""
        urns = self._sync_urns()
        pairs = [(owner, urn) for owner, us in urns.items() for urn in us]
        res = multicall([(self.VAT, self.URNS_SELECTOR + self.ILK[2:] + _word(urn)) for _, urn in pairs], block)
        out: dict[str, int] = {}
        for (owner, _), ret in zip(pairs, res):
            ink = int.from_bytes(ret[:32], "big") if ret else 0
            out[owner] = out.get(owner, 0) + ink
        return out


POSITION_SOURCES = {
    "SKY": [LockstakePosition()],
}


def position_participants(symbol: str, block="latest") -> dict[str, dict[str, int]]:
    """{address: {source_name: raw_amount}} for every non-zero position of this token."""
    out: dict[str, dict[str, int]] = {}
    for source in POSITION_SOURCES.get(symbol, []):
        for addr, amt in source.all_positions(block).items():
            if amt > 0:
                out.setdefault(addr, {})[source.name] = amt
    return out


def holdings(symbol: str, token: dict, addresses: list[str], block="latest",
             positions: dict[str, dict[str, int]] | None = None) -> dict[str, dict]:
    """
    {address: {"wallet": float, "positions": {source: float}, "total": float}}
    Pass `positions` from position_participants() to avoid re-reading them.
    """
    scale = 10 ** token["decimals"]
    wallet = balances_of(token["contract"], addresses, block)
    if positions is None:
        positions = position_participants(symbol, block) if symbol in POSITION_SOURCES else {}
    out = {}
    for a in addresses:
        a = a.lower()
        pos = {k: v / scale for k, v in positions.get(a, {}).items()}
        w = wallet[a] / scale
        out[a] = {"wallet": w, "positions": pos, "total": w + sum(pos.values())}
    return out


def positions_json(h: dict) -> str:
    return json.dumps({k: round(v, 6) for k, v in h["positions"].items()})
