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
            rank            INTEGER,              -- rank in raw Dune result before filtering
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

        -- Every raw Dune top-holder row, before any filtering. Keeping these means
        -- filter logic can change without paying for another Dune pull.
        CREATE TABLE IF NOT EXISTS raw_holder_pulls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_symbol    TEXT NOT NULL,
            pull_id         TEXT NOT NULL,        -- ISO timestamp shared by one pull
            rank            INTEGER,
            address         TEXT NOT NULL,
            dune_balance    REAL,
            UNIQUE(pull_id, token_symbol, address)
        );

        -- Result of each balanceOf() cross-check of the cohort against Dune.
        CREATE TABLE IF NOT EXISTS balance_checks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_symbol    TEXT NOT NULL,
            address         TEXT NOT NULL,
            block_number    INTEGER NOT NULL,
            dune_balance    REAL,
            chain_balance   REAL,
            rel_diff        REAL,                 -- (dune - chain) / chain
            checked_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(token_symbol, address, block_number)
        );

        CREATE INDEX IF NOT EXISTS idx_holders_token    ON holders(token_symbol);
        CREATE INDEX IF NOT EXISTS idx_snapshots_token  ON holder_snapshots(token_symbol);
        CREATE INDEX IF NOT EXISTS idx_snapshots_date   ON holder_snapshots(snapshot_date);
    """)

    # Columns added after the first cohort pull — migrate existing DBs in place.
    existing = {row[1] for row in cur.execute("PRAGMA table_info(holders)")}
    for col, ddl in [
        ("wallet_type",    "TEXT"),       # eoa | eip7702 | safe
        ("dune_balance",   "REAL"),       # balance computed by the Dune transfer-sum query
        ("verified_block", "INTEGER"),    # block balance_at_pull was read at via balanceOf()
    ]:
        if col not in existing:
            cur.execute(f"ALTER TABLE holders ADD COLUMN {col} {ddl}")
    # Pre-verification cohorts stored the Dune balance in balance_at_pull
    cur.execute("UPDATE holders SET dune_balance = balance_at_pull "
                "WHERE dune_balance IS NULL AND verified_block IS NULL")

    con.commit()
    con.close()
    print(f"Database initialised at {DB_PATH}")


if __name__ == "__main__":
    init_db()
