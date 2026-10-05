"""
Shrinks the database before it's uploaded: drops daily-history rows for addresses
that are no longer in a token's candidate universe or live holder list, then VACUUMs.

Pruned history is refilled automatically if an address rejoins (backfill and
build_cohorts only record missing days). Tokens without a stored universe are
skipped, so a half-built token keeps its rows.

Usage:
    python scripts/prune_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import DB_PATH, TOKEN_BASKET
from db.schema import get_connection


def main():
    before = Path(DB_PATH).stat().st_size
    con = get_connection()
    built = {r[0] for r in con.execute("SELECT DISTINCT token_symbol FROM universe")}
    for symbol in TOKEN_BASKET:
        if symbol not in built:
            print(f"  {symbol}: no stored universe — skipped")
            continue
        cur = con.execute(
            """DELETE FROM holdings_daily
               WHERE token_symbol = ?
                 AND address NOT IN (SELECT address FROM universe WHERE token_symbol = ? AND excluded IS NULL)
                 AND address NOT IN (SELECT address FROM holders WHERE token_symbol = ?)""",
            (symbol, symbol, symbol))
        con.commit()
        print(f"  {symbol}: removed {cur.rowcount:,} rows")
    con.execute("VACUUM")
    con.execute("ANALYZE")
    con.close()
    after = Path(DB_PATH).stat().st_size
    print(f"Database {before / 1e6:,.0f} MB → {after / 1e6:,.0f} MB")


if __name__ == "__main__":
    main()
