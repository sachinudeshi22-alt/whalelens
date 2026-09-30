import os
from dotenv import load_dotenv

load_dotenv()

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
DUNE_API_KEY      = os.getenv("DUNE_API_KEY", "")

ETHERSCAN_BASE_URL = "https://api.etherscan.io/api"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
DUNE_BASE_URL      = "https://api.dune.com/api/v1"
# Tried in order. Must be archive-capable: verify_balances.py reads historical state.
# (publicnode now rejects archive requests without a personal token.)
ETH_RPC_URLS = [
    "https://eth.drpc.org",
    "https://eth-mainnet.public.blastapi.io",
]

DB_PATH = "data/whale_tracker.db"

# How many raw top holders to pull per token before exclusion filtering.
# We fetch more than we need so the filtered cohort stays at ~50 clean addresses.
TOP_HOLDER_RAW_LIMIT = 300
TOP_HOLDER_COHORT_SIZE = 50

# Flag a holder when the Dune-derived balance and on-chain balanceOf() disagree by more than this.
BALANCE_MISMATCH_TOLERANCE = 0.01

# Token basket — 13 mid-cap ERC-20s for Phase 1.
# Format: symbol -> {contract, decimals, coingecko_id[, standard]}
# standard: "dstoken" for tokens emitting Mint/Burn instead of zero-address Transfers.
# All addresses verified on-chain via symbol() call.
TOKEN_BASKET = {
    "LINK":  {"contract": "0x514910771af9ca656af840dff83e8264ecf986ca", "decimals": 18, "coingecko_id": "chainlink"},
    "UNI":   {"contract": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984", "decimals": 18, "coingecko_id": "uniswap"},
    "AAVE":  {"contract": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9", "decimals": 18, "coingecko_id": "aave"},
    "MKR":   {"contract": "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2", "decimals": 18, "coingecko_id": "maker", "standard": "dstoken"},
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
