"""
Token held outside the wallet but still controlled by the holder.

A plain balanceOf() misses tokens a holder has staked, so staking looks like
selling. Each position source enumerates its participants and reads their
position size, and holdings() adds positions to wallet balances.

Only sources with a verified per-owner read are listed here. Current coverage:

  AAVE  — Safety Module (stkAAVE): shares × previewRedeem rate (1:1 before it existed)
  CRV   — veCRV: locked(user).amount
  LINK  — Staking v0.2 community pool: getStakerPrincipal(user)
  SUSHI — SushiBar (xSUSHI): shares × SUSHI held by the bar / xSUSHI supply
  BAL   — veBAL: locked 80BAL-20WETH pool tokens × BAL per pool token
  1INCH — st1INCH: depositors(user).amount

  Escrow participants are everyone who ever sent the token into the contract
  (plus, for transferable share tokens, everyone who ever received shares), with
  their total deposited. A position can't exceed its deposits by more than
  accrued rewards, so only participants who deposited at least
  POSITION_MIN_DEPOSIT_SHARE of supply are tracked; that floor is added to the
  completeness bound. Each source can check its coverage against the contract's
  own total (scripts/check_positions.py).

  SKY — LockstakeEngine. Each owner opens urns (Open events); the SKY locked in
        an urn is its `ink` in the Vat under the engine's ilk. When an urn picks a
        vote delegate, its SKY physically moves to a VoteDelegate and on into the
        Chief, but the urn's ink still records it — so we count ink only and never
        count Chief/VoteDelegate balances again. (As of 2026-09-30, 99.999% of the
        SKY in Chief arrived via lockstake; direct delegation is ~40k SKY and ignored.)
"""
import json

from config import POSITION_MIN_DEPOSIT_SHARE, TOKEN_BASKET
from chain.blockscout import get_logs
from chain.rpc import balances_of, latest_block, multicall
from chain.transfers import aggregate_transfers
from db.schema import get_connection

TOTAL_SUPPLY = "0x18160ddd"
BALANCE_OF = "0x70a08231"


def _word(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def _uint(ret: bytes | None, word: int = 0) -> int:
    return int.from_bytes(ret[32 * word:32 * (word + 1)], "big") if ret and len(ret) >= 32 * (word + 1) else 0


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

    def unseen_floor(self) -> float:
        return 0.0   # every owner that ever opened an urn is tracked

    def participants(self, min_position: float | None = None) -> list[str]:
        return list(self._sync_urns())   # no deposit totals for urns; all owners are cheap enough

    def unseen_above(self, min_position: float) -> float:
        return 0.0

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


class EscrowPosition:
    """
    Tokens deposited into a contract that records a per-user amount.
    Subclasses implement read(addresses, block) -> {addr: raw amount in the token's units}.
    Selectors and deploy blocks are verified (web3_sha3 / creation transaction), not guessed.
    """
    name = ""
    contract = ""        # escrow contract
    asset = ""           # token users send in (participants are its senders)
    share_token = None   # transferable receipt token, if any (its recipients are participants too)
    predecessors = ()    # (contract, deploy_block) of earlier versions whose depositors migrated here
    deploy_block = 0
    symbol = ""

    def _sync(self) -> None:
        con = get_connection()
        row = con.execute("SELECT next_block FROM position_sync WHERE source = ?", (self.name,)).fetchone()
        start, head = (row[0] if row else self.deploy_block), latest_block()
        if start <= head:
            deposits = aggregate_transfers(self.asset, start, head, to_address=self.contract)
            # Migrated stakers' tokens arrive from the old contract, not from them, so their
            # deposits into the predecessor make them participants here
            for old, old_deploy in self.predecessors:
                old_start = old_deploy if row is None else start
                for a, v in aggregate_transfers(self.asset, old_start, head, to_address=old).items():
                    deposits[a] = deposits.get(a, 0.0) + v
            if self.share_token:
                for a, v in aggregate_transfers(self.share_token, start, head).items():
                    deposits[a] = deposits.get(a, 0.0) + v
            con.executemany(
                """INSERT INTO position_deposits (source, address, deposited) VALUES (?,?,?)
                   ON CONFLICT(source, address) DO UPDATE SET deposited = deposited + excluded.deposited""",
                [(self.name, a, v) for a, v in deposits.items()])
            con.execute("INSERT OR REPLACE INTO position_sync (source, next_block) VALUES (?,?)",
                        (self.name, head + 1))
            con.commit()
        con.close()

    def floor_in_deposit_units(self) -> float:
        """Smallest total deposit worth tracking, in the units deposits are recorded in."""
        token = TOKEN_BASKET[self.symbol]
        supply = _uint(multicall([(token["contract"], TOTAL_SUPPLY)])[0]) / 10 ** token["decimals"]
        return POSITION_MIN_DEPOSIT_SHARE * supply

    def unseen_floor(self) -> float:
        """Max position (token units) of any participant we don't track."""
        return self.floor_in_deposit_units() * 1.05   # small allowance for accrued rewards

    def deposit_units(self, token_amount: float) -> float:
        """Convert a token-unit amount to the units deposits are recorded in."""
        return token_amount

    def unseen_above(self, min_position: float) -> float:
        """Max position (token units) of a participant left out by participants(min_position)."""
        return max(min_position, self.floor_in_deposit_units()) * 1.05   # rewards allowance

    def participants(self, min_position: float | None = None) -> list[str]:
        """Participants whose total deposits could make their position ≥ min_position (token units)."""
        self._sync()
        floor = self.floor_in_deposit_units()
        if min_position is not None:
            floor = max(floor, self.deposit_units(min_position))
        con = get_connection()
        rows = con.execute("SELECT address FROM position_deposits WHERE source = ? AND deposited >= ?",
                           (self.name, floor)).fetchall()
        con.close()
        zero = "0x" + "0" * 40
        return [r[0] for r in rows if r[0] != zero]

    def all_positions(self, block="latest") -> dict[str, int]:
        return self.read(self.participants(), block)

    def read(self, addresses: list[str], block) -> dict[str, int]:
        raise NotImplementedError


class _PerUserCall(EscrowPosition):
    selector = ""
    word = 0

    def read(self, addresses, block):
        res = multicall([(self.contract, self.selector + _word(a)) for a in addresses], block)
        return {a: _uint(r, self.word) for a, r in zip(addresses, res)}


class VeCRVPosition(_PerUserCall):
    name, symbol = "veCRV", "CRV"
    contract = "0x5f3b5dfeb7b28cdbd7faba78963ee202a494e2a2"
    asset = TOKEN_BASKET["CRV"]["contract"]
    selector = "0xcbf9fe5f"   # locked(address) -> (int128 amount, uint256 end)
    deploy_block = 10647812


class LinkStakingPosition(_PerUserCall):
    name, symbol = "link_staking", "LINK"
    contract = "0xbc10f2e862ed4502144c7d632a3459f49dfcdb5e"
    asset = TOKEN_BASKET["LINK"]["contract"]
    selector = "0xe0d307e0"   # getStakerPrincipal(address) -> uint256
    deploy_block = 18572190
    # Staking v0.1; its stakers migrated into v0.2, and their LINK arrived from this contract
    predecessors = (("0x3feb1e09b4bb0e7f0387cee092a52e85797ab889", 16083969),)


class St1inchPosition(_PerUserCall):
    name, symbol = "st1inch", "1INCH"
    contract = "0x9a0c8ff858d273f57072d714bca7411d717501d7"
    asset = TOKEN_BASKET["1INCH"]["contract"]
    selector = "0xeed75f6d"   # depositors(address) -> (uint40 lockTime, uint40 unlockTime, uint176 amount)
    word = 2
    deploy_block = 16241691


class _ShareToken(EscrowPosition):
    def rate(self, block) -> float:
        raise NotImplementedError

    def read(self, addresses, block):
        res = multicall([(self.share_token, BALANCE_OF + _word(a)) for a in addresses], block)
        r = self.rate(block)
        return {a: int(_uint(x) * r) for a, x in zip(addresses, res)}


class XSushiPosition(_ShareToken):
    name, symbol = "xsushi", "SUSHI"
    contract = share_token = "0x8798249c2e607446efb7ad49ec89dd1865ff4272"
    asset = TOKEN_BASKET["SUSHI"]["contract"]
    deploy_block = 10801571

    def rate(self, block):
        held, supply = multicall([(self.asset, BALANCE_OF + _word(self.contract)),
                                  (self.share_token, TOTAL_SUPPLY)], block)
        return _uint(held) / _uint(supply) if _uint(supply) else 0.0


class StkAavePosition(_ShareToken):
    name, symbol = "stkAAVE", "AAVE"
    contract = share_token = "0x4da27a545c0c5b758a6ba100e3a049001de870f5"
    asset = TOKEN_BASKET["AAVE"]["contract"]
    deploy_block = 10927018

    def rate(self, block):
        ret = multicall([(self.share_token, "0x4cdad506" + (10 ** 18).to_bytes(32, "big").hex())], block)[0]
        return _uint(ret) / 1e18 if ret else 1.0   # previewRedeem(1e18); 1:1 before it existed


class VeBALPosition(EscrowPosition):
    name, symbol = "veBAL", "BAL"
    contract = "0xc128a9954e6c874ea3d62ce62b468ba073093f25"
    asset = "0x5c6ee304399dbdb9c8ef030ab642b10820db8f56"   # B-80BAL-20WETH pool token
    vault = "0xba12222222228d8ba445958a75a0704d566bf2c8"
    pool_id = "0x5c6ee304399dbdb9c8ef030ab642b10820db8f56000200000000000000000014"
    deploy_block = 14457013

    def bal_per_bpt(self, block) -> float:
        bal = TOKEN_BASKET["BAL"]["contract"]
        held, supply = multicall([(bal, BALANCE_OF + _word(self.vault)), (self.asset, TOTAL_SUPPLY)], block)
        # The Vault holds BAL for every pool; read this pool's balance instead
        ret = multicall([(self.vault, "0xf94d4668" + self.pool_id[2:])], block)[0]   # getPoolTokens(bytes32)
        tokens_off = _uint(ret, 0) // 32
        n = _uint(ret, tokens_off)
        tokens = ["0x" + ret[32 * (tokens_off + 1 + i) + 12:32 * (tokens_off + 2 + i)].hex() for i in range(n)]
        bal_off = _uint(ret, 1) // 32
        balances = [_uint(ret, bal_off + 1 + i) for i in range(n)]
        pool_bal = balances[tokens.index(bal.lower())]
        return pool_bal / _uint(supply) if _uint(supply) else 0.0

    def floor_in_deposit_units(self) -> float:
        return super().floor_in_deposit_units() / max(self.bal_per_bpt("latest"), 1e-9)

    def deposit_units(self, token_amount: float) -> float:
        return token_amount / max(self.bal_per_bpt("latest"), 1e-9)

    def unseen_floor(self) -> float:
        return super().floor_in_deposit_units() * 1.05

    def unseen_above(self, min_position: float) -> float:
        return max(min_position, super().floor_in_deposit_units()) * 1.05

    def read(self, addresses, block):
        res = multicall([(self.contract, "0xcbf9fe5f" + _word(a)) for a in addresses], block)
        r = self.bal_per_bpt(block)
        return {a: int(_uint(x) * r) for a, x in zip(addresses, res)}


POSITION_SOURCES = {
    "SKY": [LockstakePosition()],
    "CRV": [VeCRVPosition()],
    "LINK": [LinkStakingPosition()],
    "1INCH": [St1inchPosition()],
    "SUSHI": [XSushiPosition()],
    "AAVE": [StkAavePosition()],
    "BAL": [VeBALPosition()],
}


def unseen_position_floor(symbol: str) -> float:
    return sum(s.unseen_floor() for s in POSITION_SOURCES.get(symbol, []))


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
