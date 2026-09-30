# Methodology

How the tracker decides who counts as a whale, what they hold, and who is an insider.
Every rule below is implemented in code; file references point to where.

## 1. Candidate holders

For each token we take the top 300 holders by wallet balance from
[Blockscout](https://eth.blockscout.com) (`scripts/fetch_holders.py`).

For tokens whose supply is largely staked (currently SKY), we also add the 200
largest staked positions, because a wallet-balance list can't see them
(`chain/positions.py`).

## 2. Exclusions

A candidate is dropped when:

- **It's a known exchange or infrastructure address.** Either it is on our manual
  list (`KNOWN_EXCLUSIONS` in `config.py`), or one of its public labels contains an
  exchange keyword (`EXCLUDE_LABEL_KEYWORDS`): exchange hot, cold and deposit
  wallets for Binance, Coinbase, Kraken, OKX and others. Labels come from
  Blockscout and the [Open Labels Initiative](https://www.openlabelsinitiative.org).
- **It's a contract that isn't a wallet.** We keep plain wallets (EOAs), wallets
  that use an EIP-7702 delegation, and Safe multisigs. We drop everything else:
  pools, vaults, staking contracts, bridges (`chain/classify.py`).
- **It holds nothing on-chain.** See section 3.

## 3. Holdings are verified on-chain

Third-party balance data is wrong often enough that we never use it directly.
Examples we found: phantom balances for MKR, SNX, SUSHI, COMP and ENS on Blockscout,
and MKR balances overstated about 12x in transfer-sum data. That happened because
MKR records burns with a non-standard event.

So every holding is re-read from an Ethereum node at a single pinned block:

- **Wallet balance:** `balanceOf(holder)` on the token contract.
- **Staked positions:** SKY locked in the Sky LockstakeEngine is read per owner from
  the Maker Vat (the `ink` of each of the owner's urns). SKY that a lockstake urn
  delegates for governance moves on into the Chief contract, but the urn's `ink`
  still records it, so it is counted once. Direct delegation that bypasses lockstake
  held about 40k SKY on 2026-09-30 (under 0.001% of Chief deposits) and is not counted.

A holder's total is wallet balance + staked positions. Holders are ranked by that total.

**Completeness check.** Any holder missing from our candidate lists holds at most the
last listed wallet balance plus the smallest staked position we considered. If our
smallest whale holds more than that sum, nobody outside the lists could belong in the
cohort. Each run prints whether this check passed.

## 4. Whales vs insiders

Insiders are wallets holding project-controlled or project-allocated supply: team,
investors, treasury, vesting. We **track them separately rather than dropping them**,
because insiders selling while outside whales accumulate is a signal in itself.

A holder is an insider when (`chain/insiders.py`):

1. **One of its own public labels** matches an insider keyword (treasury, vesting,
   foundation, team, deployer, investor, non-circulating…), or
2. **its first inbound transfer of the token** came from a known project allocation
   contract (`INSIDER_SOURCES`, where each entry records its evidence) or from an
   address whose label matches an insider keyword, or
3. **it is a Safe sharing at least half its signers** with an insider Safe.

The stored reason for every insider is published alongside it.

Funders that say nothing about insider status, such as the MKR→SKY converter and
DEX settlement contracts, are listed in `NEUTRAL_FUNDERS`.

If a holder has more than 500 inbound transfers, we don't trace its first funder.
It is recorded as "funding unknown", not guessed.

## 5. Entities

Safes that share at least half of their signers are grouped as one entity
(`entity_id`). One owner splitting holdings across several wallets otherwise looks
like several independent whales.

**Not yet handled:** grouping plain wallets (EOAs) that belong to the same owner.

## 6. Known limitations

- **Ethereum mainnet only.** Tokens bridged to L2s aren't counted.
- **Staked positions are covered for SKY only.** Other staking systems (stkAAVE,
  veCRV, LINK staking) aren't counted yet, so staking in those can look like a
  balance drop.
- **Label coverage is incomplete.** An unlabelled exchange or team wallet will pass
  the filters. Insider detection is conservative: a wallet is marked insider only
  with evidence, so some insiders will appear as whales.
- **Market makers** aren't separated from other whales yet.

Nothing here is financial advice.
