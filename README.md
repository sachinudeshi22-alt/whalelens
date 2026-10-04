# WhaleLens

What the largest independent holders, and the insiders, of 13 mid-cap DeFi tokens
are doing, read directly from Ethereum.

Most "whale trackers" list today's top addresses. WhaleLens tries to answer the
harder question of whether the people who actually hold these tokens are adding or
reducing. That takes several things a top-holder list doesn't do:

- **Every balance is verified on-chain.** Third-party holder data is used only to
  find candidates.
- **Staked tokens count as held** (SKY lockstake, veCRV, stkAAVE, LINK staking,
  veBAL, xSUSHI, st1INCH), so staking isn't mistaken for selling.
- **Exchanges, contracts and exchange-like wallets are removed**, using public
  labels plus transfer behaviour.
- **Insiders are tracked separately**: team, investor, treasury and vesting
  wallets, each with the evidence for its classification.
- **Point-in-time cohorts.** A past date's whales are that date's top 50, never
  today's top 50 looked at backwards, with a per-date proof that no wallet outside
  the candidate set could have qualified.

Tokens: LINK, UNI, AAVE, SKY, LDO, CRV, ENS, COMP, SNX, GRT, 1INCH, SUSHI, BAL.

Every rule and known limitation is in [docs/METHODOLOGY.md](docs/METHODOLOGY.md).
Not financial advice.

## How it runs

A GitHub Actions workflow ([.github/workflows/daily.yml](.github/workflows/daily.yml))
runs [scripts/run_daily.py](scripts/run_daily.py) every day at 00:30 UTC:

1. Record the new day's holdings: `backfill_history.py`
2. Extend the year-long transfer scan: `scan_transfers.py`
3. Refresh holder lists on Mondays: `fetch_holders.py`
4. Rebuild point-in-time cohorts: `build_cohorts.py`
5. Export the static site to `site/`: `export_site.py`

The SQLite database is too large for git, so it lives as an asset on this repo's
`data` release. The site is published with GitHub Pages.

## Running locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # add ETH_RPC_URL (an Alchemy endpoint) and BLOCKSCOUT_API_KEY
.venv/bin/python scripts/run_daily.py
.venv/bin/python -m http.server 8765 --directory site
```

Data sources: Ethereum mainnet via an archive RPC node (Alchemy), plus
[Blockscout](https://eth.blockscout.com) and the
[Open Labels Initiative](https://www.openlabelsinitiative.org) for public address labels.
