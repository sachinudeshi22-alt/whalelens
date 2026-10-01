import sqlite3
from config import DB_PATH


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    con = get_connection()
    cur = con.cursor()

    cur.executescript("""
        -- Tokens we track (the basket).
        CREATE TABLE IF NOT EXISTS tokens (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT NOT NULL UNIQUE,
            contract        TEXT NOT NULL UNIQUE,
            decimals        INTEGER NOT NULL DEFAULT 18,
            coingecko_id    TEXT,
            added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- The filtered cohort of top non-exchange, non-contract holders per token.
        -- Rebuilt periodically (e.g. weekly) by fetch_holders.py.
        CREATE TABLE IF NOT EXISTS holders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_symbol    TEXT NOT NULL,
            address         TEXT NOT NULL,
            rank            INTEGER,              -- rank in the raw source result before filtering
            balance_at_pull REAL,                 -- balance when the cohort was last pulled
            pulled_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(token_symbol, address)
        );

        -- Daily snapshots of the cohort's aggregate token balance per token.
        -- One row per (token, date). snapshot_holdings.py writes this every day.
        CREATE TABLE IF NOT EXISTS holder_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            token_symbol        TEXT NOT NULL,
            snapshot_date       TEXT NOT NULL,    -- ISO date: YYYY-MM-DD
            cohort_size         INTEGER,          -- number of holders in cohort that day
            aggregate_balance   REAL,             -- sum of all cohort balances
            snapshotted_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(token_symbol, snapshot_date)
        );

        -- Every raw top-holder row from the holder source, before any filtering. Keeping these means
        -- filter logic can change without another pull.
        CREATE TABLE IF NOT EXISTS raw_holder_pulls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_symbol    TEXT NOT NULL,
            pull_id         TEXT NOT NULL,        -- ISO timestamp shared by one pull
            source          TEXT,                 -- blockscout | dune
            rank            INTEGER,
            address         TEXT NOT NULL,
            source_balance  REAL,
            labels          TEXT,                 -- JSON list of public labels, if the source has them
            UNIQUE(pull_id, token_symbol, address)
        );

        -- Result of each balanceOf() cross-check of the cohort against the holder source.
        CREATE TABLE IF NOT EXISTS balance_checks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_symbol    TEXT NOT NULL,
            address         TEXT NOT NULL,
            block_number    INTEGER NOT NULL,
            source_balance  REAL,
            chain_balance   REAL,
            rel_diff        REAL,                 -- (source - chain) / chain
            checked_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(token_symbol, address, block_number)
        );

        -- Staking positions (e.g. SKY lockstake urns), cached from Open events.
        CREATE TABLE IF NOT EXISTS position_urns (
            source          TEXT NOT NULL,        -- position source name, e.g. lockstake
            owner           TEXT NOT NULL,
            idx             INTEGER NOT NULL,
            urn             TEXT NOT NULL,
            block_number    INTEGER,
            UNIQUE(source, owner, idx)
        );

        -- First inbound transfer of each token per address. It never changes, so it's
        -- traced once and reused (saves most Blockscout credits on daily refreshes).
        CREATE TABLE IF NOT EXISTS first_funders (
            token_symbol    TEXT NOT NULL,
            address         TEXT NOT NULL,
            funder          TEXT,                 -- NULL when untraceable (see note)
            funder_labels   TEXT,                 -- JSON list, as seen when traced
            block_number    INTEGER,
            note            TEXT,                 -- e.g. "too many transfers to trace"
            traced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(token_symbol, address)
        );

        CREATE INDEX IF NOT EXISTS idx_holders_token    ON holders(token_symbol);
        CREATE INDEX IF NOT EXISTS idx_snapshots_token  ON holder_snapshots(token_symbol);
        CREATE INDEX IF NOT EXISTS idx_snapshots_date   ON holder_snapshots(snapshot_date);
    """)

    # Columns added after the first cohort pull — migrate existing DBs in place.
    def columns(table):
        return {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}

    # dune_balance was renamed once the holder source became pluggable
    for table in ("holders", "raw_holder_pulls", "balance_checks"):
        if "dune_balance" in columns(table):
            cur.execute(f"ALTER TABLE {table} RENAME COLUMN dune_balance TO source_balance")

    for table, col, ddl in [
        ("holders",          "wallet_type",    "TEXT"),     # eoa | eip7702 | safe
        ("holders",          "source_balance", "REAL"),     # balance reported by the holder source
        ("holders",          "verified_block", "INTEGER"),  # block balance_at_pull was read at via balanceOf()
        ("holders",          "labels",         "TEXT"),     # JSON list of public labels
        ("holders",          "wallet_balance", "REAL"),     # balanceOf() part of balance_at_pull
        ("holders",          "positions",      "TEXT"),     # JSON {source: amount} staked outside the wallet
        ("holders",          "category",       "TEXT"),     # whale | insider
        ("holders",          "insider_reason", "TEXT"),     # why it was classed insider (published methodology)
        ("holders",          "first_funder",   "TEXT"),     # sender of the first inbound transfer of the token
        ("holders",          "entity_id",      "TEXT"),     # shared id for Safes controlled by the same signers
        ("raw_holder_pulls", "source",         "TEXT"),
        ("raw_holder_pulls", "labels",         "TEXT"),
    ]:
        if col not in columns(table):
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # Pre-verification cohorts stored the Dune balance in balance_at_pull
    cur.execute("UPDATE holders SET source_balance = balance_at_pull "
                "WHERE source_balance IS NULL AND verified_block IS NULL")

    con.commit()
    con.close()
    print(f"Database initialised at {DB_PATH}")


if __name__ == "__main__":
    init_db()
