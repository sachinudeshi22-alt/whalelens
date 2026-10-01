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
- **It behaves like an exchange or market maker.** An address with 2,000 or more
  transfers of the token in a year is excluded (`HIGH_ACTIVITY_TRANSFERS`). For
  comparison, the median whale makes a handful of transfers a year. This catches
  unlabelled exchange and bot wallets that the label filter misses.
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
   vested, foundation, team, investor, non-circulating, or a project-specific
   deployer such as "Lido: Deployer 1"), or
2. **its first inbound transfer of the token** came from a known project allocation
   contract (`INSIDER_SOURCES`, where each entry records its evidence) or from an
   address whose label matches an insider keyword, or
3. **it is a Safe sharing at least half its signers** with an insider Safe.

The stored reason for every insider is published alongside it.

Funders that say nothing about insider status, such as the MKR→SKY converter and
DEX settlement contracts, are listed in `NEUTRAL_FUNDERS`. Generic tags that
Blockscout applies to huge numbers of unrelated addresses (for example "Contract
Deployer", which marks any address that ever deployed a contract) are never treated
as evidence (`GENERIC_LABELS`).

If a holder has more than 500 inbound transfers, we don't trace its first funder.
It is recorded as "funding unknown", not guessed.

## 5. Point-in-time whales

"Whales" on any past date are **that date's** 50 largest independent wallets, not
today's 50 looked at backwards. Looking back at today's top holders builds
accumulation into the result, because they are the top holders *because* they ended
up with the most. Changes over a window always follow the wallets that were whales
at the **start** of that window, so a whale that sold out still counts against the
group (`scripts/build_cohorts.py`).

To find past whales that no longer appear in today's holder list, we scan every
transfer of the token over the period. The candidate universe for each date is:

- today's top 300 holders, plus
- every address that has ever staked (for tokens with staking positions), plus
- every address that sent at least a quarter of today's smallest whale holding
  during the period.

**Completeness proof, per date.** Take an address outside the universe. Its holdings
on any past date equal its holdings now plus what it sent since, minus what it
received. Its holdings now are below the smallest top-300 balance, and what it sent
is below the threshold. So on every date it held less than the sum of those two
numbers. If that date's 50th whale held more than the sum, no address outside the
universe could have made the top 50. We check this for every date and show on each
token page how many days pass.

## 6. Entities

Safes that share at least half of their signers are grouped as one entity
(`entity_id`). One owner splitting holdings across several wallets otherwise looks
like several independent whales.

**Not yet handled:** grouping plain wallets (EOAs) that belong to the same owner.

## 7. Known limitations

- **Ethereum mainnet only.** Tokens bridged to L2s aren't counted.
- **Staked positions are covered for SKY only.** Other staking systems (stkAAVE,
  veCRV, LINK staking) aren't counted yet, so staking in those can look like a
  balance drop.
- **Label coverage is incomplete.** An unlabelled exchange or team wallet will pass
  the filters. Insider detection is conservative: a wallet is marked insider only
  with evidence, so some insiders will appear as whales.
- **Low-activity market makers and OTC desks** that make few, large transfers aren't
  separated from other whales yet.

Nothing here is financial advice.
