import os
from dotenv import load_dotenv

load_dotenv()

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
DUNE_API_KEY      = os.getenv("DUNE_API_KEY", "")
BLOCKSCOUT_API_KEY = os.getenv("BLOCKSCOUT_API_KEY", "")  # optional; raises Blockscout rate limits

ETHERSCAN_BASE_URL = "https://api.etherscan.io/api"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
DUNE_BASE_URL      = "https://api.dune.com/api/v1"
BLOCKSCOUT_BASE_URL = "https://eth.blockscout.com/api/v2"
# Blockscout PRO API (needs BLOCKSCOUT_API_KEY from dev.blockscout.com). 5 req/s,
# 20 credits per request from a 100k allowance; the client falls back to the free
# endpoint above once credits drop below the reserve.
BLOCKSCOUT_PRO_URL = "https://api.blockscout.com/1/api/v2"
BLOCKSCOUT_CREDIT_RESERVE = 5_000

# Where fetch_holders.py gets the candidate top-holder list:
#   "blockscout" — free, no key, current balances + public address labels
#   "dune"       — transfer-sum SQL; needs a Dune plan that can run queries
#                  (the account went read-only on the free tier by 2026-09)
HOLDER_SOURCE = "blockscout"
# (url, max calls per JSON-RPC batch), tried in order. Must be archive-capable:
# verify_balances.py and the history backfill read historical state.
# ETH_RPC_URL (e.g. an Alchemy endpoint) goes first when set; the free public
# endpoints are fallbacks. (publicnode rejects archive requests without a token.)
ETH_RPC_URL = os.getenv("ETH_RPC_URL", "").strip()
ETH_RPC_ENDPOINTS = ([(ETH_RPC_URL, 200)] if ETH_RPC_URL else []) + [
    ("https://eth.drpc.org", 25),
    ("https://eth-mainnet.public.blastapi.io", 25),
]

DB_PATH = "data/whale_tracker.db"

# How many raw top holders to pull per token before exclusion filtering.
# We fetch more than we need so the filtered cohort stays at ~50 clean addresses.
TOP_HOLDER_RAW_LIMIT = 300
TOP_HOLDER_COHORT_SIZE = 50
# For tokens with staking positions (chain/positions.py): how many of the largest
# stakers to add as candidates alongside the wallet top-holder list.
POSITION_CANDIDATE_LIMIT = 200

# Point-in-time cohorts (scripts/build_cohorts.py): addresses that sent at least this
# fraction of today's smallest whale holding during the scanned period join the universe.
UNIVERSE_SENT_FRACTION = 0.25
# Addresses with at least this many transfers of the token in the scanned period
# (~a year) behave like exchanges or market makers, not holders, and are excluded.
HIGH_ACTIVITY_TRANSFERS = 2000

# Flag a holder when the source's balance and on-chain balanceOf() disagree by more than this.
BALANCE_MISMATCH_TOLERANCE = 0.01

# Token basket — 13 mid-cap ERC-20s for Phase 1.
# (MKR, if ever re-added, needs "standard": "dstoken" — it emits Mint/Burn, not zero-address Transfers.)
# Format: symbol -> {contract, decimals, coingecko_id[, standard]}
# standard: "dstoken" for tokens emitting Mint/Burn instead of zero-address Transfers.
# All addresses verified on-chain via symbol() call.
TOKEN_BASKET = {
    "LINK":  {"contract": "0x514910771af9ca656af840dff83e8264ecf986ca", "decimals": 18, "coingecko_id": "chainlink"},
    "UNI":   {"contract": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984", "decimals": 18, "coingecko_id": "uniswap"},
    "AAVE":  {"contract": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9", "decimals": 18, "coingecko_id": "aave"},
    # SKY replaced MKR on 2026-09-30: ~92% of MKR had converted (1 MKR = 24,000 SKY) and
    # late conversions pay a growing penalty, so MKR flows mostly reflect conversion, not conviction.
    # ~44% of SKY sits in lockstake — see chain/positions.py for how staked SKY is attributed.
    "SKY":   {"contract": "0x56072c95faa701256059aa122697b133aded9279", "decimals": 18, "coingecko_id": "sky"},
    "LDO":   {"contract": "0x5a98fcbea516cf06857215779fd812ca3bef1b32", "decimals": 18, "coingecko_id": "lido-dao"},
    "CRV":   {"contract": "0xd533a949740bb3306d119cc777fa900ba034cd52", "decimals": 18, "coingecko_id": "curve-dao-token"},
    "ENS":   {"contract": "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72", "decimals": 18, "coingecko_id": "ethereum-name-service"},
    "COMP":  {"contract": "0xc00e94cb662c3520282e6f5717214004a7f26888", "decimals": 18, "coingecko_id": "compound-governance-token"},
    "SNX":   {"contract": "0xc011a73ee8576fb46f5e1c5751ca3b9fe0af2a6f", "decimals": 18, "coingecko_id": "havven"},
    "GRT":   {"contract": "0xc944e90c64b2c07662a292be6244bdf05cda44a7", "decimals": 18, "coingecko_id": "the-graph"},
    "1INCH": {"contract": "0x111111111117dc0aa78b770fa6a738034120c302", "decimals": 18, "coingecko_id": "1inch"},
    "SUSHI": {"contract": "0x6b3595068778dd592e39a122f4f5a5cf09c90fe2", "decimals": 18, "coingecko_id": "sushi"},
    "BAL":   {"contract": "0xba100000625a3754423978a60c9317c58a424e3d", "decimals": 18, "coingecko_id": "balancer"},
}

# Drop a holder when any of its public labels (from Blockscout) contains one of these,
# case-insensitive. Catches exchange hot/cold/deposit wallets and team supply wallets
# that KNOWN_EXCLUSIONS doesn't list.
EXCLUDE_LABEL_KEYWORDS = [
    "exchange", "hot wallet", "cold wallet", "deposit address",
    "binance", "coinbase", "kraken", "okx", "bybit", "bitfinex", "gemini",
    "robinhood", "crypto.com", "bithumb", "upbit", "htx", "huobi", "kucoin",
    "gate.io", "bitget", "mexc", "paxos", "bitpanda", "bitstamp",
]

# Insider = project-controlled or project-allocated supply (team, investors, treasury,
# vesting). Insiders are NOT dropped: they're tracked as a separate group, because
# "insiders selling while outside whales buy" is itself a signal.
# A holder is an insider when (checked in chain/insiders.py):
#   1. one of its own public labels matches INSIDER_LABEL_KEYWORDS, or
#   2. its first inbound transfer of the token came from an address in the token's
#      INSIDER_SOURCES, or from an address whose label matches INSIDER_LABEL_KEYWORDS, or
#   3. it is a Safe sharing at least half its signers with an insider Safe.
# Keywords match whole words, case-insensitive.
INSIDER_LABEL_KEYWORDS = [
    "treasury", "vesting", "vested", "noncirculating", "non-circulating", "foundation",
    "team", "deployer", "investor", "investors", "token manager", "multisig: team",
]
# Generic tags Blockscout puts on huge numbers of unrelated addresses (any address
# that ever deployed a contract is a "Contract Deployer"). Never insider evidence;
# a deployer label only counts when project-specific, e.g. "Lido: Deployer 1".
GENERIC_LABELS = {"contract deployer", "beacon depositor", "ethereum torchbearer"}

# Per-token addresses that distribute insider allocations. Every entry needs evidence.
INSIDER_SOURCES = {
    "LDO": {
        # Lido DAO Aragon app that issued LDO allocations: sent 50M LDO each to three
        # Safes on 2020-12-17 (token launch) and round-number grants (10M, 7x5M, 2M, 1.93M)
        # to ten Safes sharing the same 5 signers on 2026-01-01.
        "0xf73a1260d222f447210581ddf212d915c09a3249",
    },
    "CRV": {
        # Curve founder Michael Egorov's wallet (public label; also Curve's deployer).
        # First CRV from here means a team allocation or a founder sale.
        "0x7a16ff8270133f063aab6c9977183d9e72835428",
    },
}

# Funders whose transfers say nothing about insider status (conversions, DEX settlement).
# suggest_insider_sources() skips these so REVIEW output stays actionable.
NEUTRAL_FUNDERS = {
    "0xa1ea1ba18e88c381c724a75f23a130420c403f9a",  # Sky: MKR→SKY converter (MkrSky)
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41",  # CoW Protocol GPv2Settlement
    "0xba12222222228d8ba445958a75a0704d566bf2c8",  # Balancer Vault
    "0x000000000004444c5dc75cb358380d2e3de08a90",  # Uniswap v4 PoolManager
    # Unlabelled exchange hot wallet (contract): takes many small deposits across dozens
    # of tokens from distinct addresses and pays out via batched withdrawals (seen 2026-09-30).
    # First funder of 25 LINK and 3 SKY top holders — i.e. they withdrew from this exchange.
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43",
}

# Addresses to always exclude from the holder cohort.
# Covers: exchanges, bridges, Chainlink staking contracts, LP contracts, zero address.
# Expand this list as you identify more contracts in the top-holder results.
KNOWN_EXCLUSIONS = {
    # Zero / burn addresses
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",  # EVM dead address — found holding 106M UNI
    # AAVE migration artifact — impossible balance (~80 quadrillion), likely LEND->AAVE replay error
    "0x3d16ee6d46edb674e728b5923e2ecac4092f5920",
    # Chainlink staking v0.1
    "0x3feb1e09b4bb0e7f0387cee092a52e85797ab889",
    # Binance hot wallets
    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance 14
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",  # Binance 15
    "0xf977814e90da44bfa03b6295a0616a897441acec",  # Binance 8 — found in LINK top-150
    # LINK non-circulating / team allocation wallets — confirmed via Etherscan
    # (staking, treasury, noncirculating supply labels)
    "0x0dffd343c2d3460a7ead2797a687304beb394ce0",
    "0x5a8e77bc30948cc9a51ae4e042d96e145648bb4c",
    "0x7594eb0ca0a7f313befd59afe9e95c2201a443e4",
    "0x8652fb672253607c0061677bdcafb77a324de081",
    "0x76287e0f7b107d1c9f8f01d5afac314ea8461a04",
    "0xe0b66bfc7344a80152bfec954942e2926a6fca80",
    "0x9bbb46637a1df7cadec2afca19c2920cddcc8db8",
    # Coinbase Custody
    "0x503828976d22510aad0201ac7ec88293211d23da",
    # Kraken
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2",
    # Gemini
    "0xd24400ae8bfebb18ca49be86258a3c749cf46853",
    # Uniswap v2 router
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
    # Uniswap v3 factory / pool addresses are discovered at runtime;
    # add them to this set as you spot them in raw results.
}
