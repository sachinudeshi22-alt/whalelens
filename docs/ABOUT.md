# About WhaleLens

WhaleLens tracks what the largest independent holders of 13 mid-cap DeFi tokens are
doing, and separately what each project's own insiders are doing. Every number is read
directly from Ethereum.

You can find a list of a token's biggest wallets on any block explorer. That list
won't tell you whether those holders are actually adding or reducing. It's full of
exchanges, team wallets and staking contracts. It misses tokens that are staked. And
a list of *today's* top holders makes the past look like accumulation, because those
wallets are at the top precisely because they ended up holding the most. WhaleLens is
built to answer the real question despite all of that.

## What it has found

Figures below are as of 6 October 2026 unless dated otherwise. Each token page shows
the current numbers.

- **Synthetix minted 236 million new SNX in a single transaction.** On 6 July 2026,
  total SNX supply rose 68.6%. WhaleLens traced it to the protocol's governance
  multisig, which minted 236,464,356 SNX under
  [SIP-423](https://sips.synthetix.io/sips/sip-423). That proposal retires the sUSD
  stablecoin and pays holders 4 SNX per sUSD. The new tokens sit in a reserve that's
  locked for a year, then vested over the following year, so roughly 236 million SNX
  begins flowing to former sUSD holders from mid-2027.
  [SNX page](index.html#/t/SNX)

- **Insiders hold more LDO than the 50 largest outside whales do, and outside whales
  have been selling.** 19 wallets traced to Lido's own allocation contracts, vesting
  and treasury hold 28.3% of LDO supply. The 50 largest independent wallets hold
  14.5%. Over 90 days, the wallets that were whales at the start reduced their LDO by
  23%: 17 of them sold and 5 added. [LDO page](index.html#/t/LDO)

- **BAL insiders are growing while outside whales sell.** 53 insider wallets, most of
  them labelled "Balancer Vested Shareholders", hold 28.2% of BAL. Over the past 30
  days insider holdings rose 8.1% as vested allocations unlocked. Over the same
  period, 26 of the 50 starting whales reduced their holdings and only one added.
  [BAL page](index.html#/t/BAL)

- **Most SKY whales hold their tokens staked.** 38 of the 50 largest SKY holders keep
  most of their SKY locked in Sky's staking engine. A wallet-balance view misses almost
  all of them, and every time a holder stakes, it looks like a sale.
  [SKY page](index.html#/t/SKY)

- **The naive view gets the direction wrong.** On 1 October 2026, looking back at
  BAL's *current* top 50 holders suggested they had accumulated 28% in 30 days.
  Following the wallets that were actually the top 50 at the start of that window
  showed a 3.5% *decline*. The difference was look-ahead bias, plus vested insider
  wallets being counted as whales.

- **Raw holder lists are unreliable.** In an early version of the cohort, built from
  standard top-holder data, 30% of the wallets were exchange or deposit addresses.
  Third-party balance data overstated one token's holdings 12-fold, because it missed
  a non-standard burn event. That's why every balance here is re-read from the chain.

## What makes it different

1. **Verified on-chain.** Holder lists from third parties are used only to find
   candidates. Every balance is read from an Ethereum node at a fixed block.
2. **Staked tokens count.** Positions in seven staking systems are added to wallet
   balances: SKY lockstake, veCRV, stkAAVE, LINK staking, veBAL, xSUSHI and st1INCH.
3. **Exchanges and exchange-like wallets are removed.** This uses public labels plus
   behaviour. A wallet making thousands of transfers a year isn't a long-term holder.
4. **Insiders are a separate group.** Team, investor, treasury and vesting wallets are
   identified from evidence (labels, who funded them, shared multisig signers), and
   the reason is shown for each one.
5. **No look-ahead.** A past date's whales are that date's top 50. Every date carries
   a completeness proof showing that no wallet outside the candidate set could have
   ranked in it.

## How it's built

A Python pipeline runs every day on GitHub Actions and publishes this static site to
GitHub Pages. The whole thing runs on free tiers.

- **Chain data:** an archive Ethereum node (Alchemy). Thousands of balance reads are
  batched into single calls with Multicall3, and a year of token transfers is
  scanned to find past whales that today's lists no longer show.
- **Labels and holder candidates:** Blockscout and the Open Labels Initiative.
- **Storage:** SQLite. It holds over a million daily holdings rows, a year of
  per-address transfer totals, and point-in-time cohorts for every date.
- **Site:** plain HTML, CSS and JavaScript, with hand-built SVG charts, light and
  dark mode, and a table view behind every chart.

The code and the full [methodology](methodology.html) are public on
[GitHub](https://github.com/sachinudeshi22-alt/whalelens).

## Limits

WhaleLens covers Ethereum mainnet only, so tokens bridged to other chains aren't
counted. Wallets that belong to the same owner are grouped only when they're Safes
with shared signers. Label coverage is incomplete, so an unlabelled exchange or team
wallet can still pass the filters. And whether whale or insider moves predict prices
hasn't been tested yet. That's on the roadmap, and the results will be published
whichever way they go. Nothing here is financial advice.

## Built by

[Sachin Udeshi](https://github.com/sachinudeshi22-alt)
