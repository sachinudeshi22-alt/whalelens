"""
Exports the public site's data: site/data/overview.json and site/data/<SYMBOL>.json,
plus site/methodology.html rendered from docs/METHODOLOGY.md.

All "whale" history uses point-in-time cohorts (cohort_daily), never today's
cohort looked at backwards. Tokens without built cohorts are listed as pending.

Usage:
    python scripts/export_site.py
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import markdown

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config import TOKEN_BASKET
from db.schema import init_db, get_connection
from chain.rpc import multicall

SITE = ROOT / "site"
TOTAL_SUPPLY = "0x18160ddd"
_MOVE = 0.01                 # members moving less than this count as flat
_SUPPLY_JUMP = 0.05          # day-over-day supply change worth a note
WINDOWS = (7, 30, 90)


def supply_series(con, symbol: str) -> dict[str, float]:
    """Total supply at each daily block, cached in supply_daily."""
    have = dict(con.execute("SELECT date, supply FROM supply_daily WHERE token_symbol = ?", (symbol,)))
    missing = [(d, b) for d, b in con.execute("SELECT date, block_number FROM daily_blocks ORDER BY date")
               if d not in have]
    token = TOKEN_BASKET[symbol]
    for d, b in missing:
        ret = multicall([(token["contract"], TOTAL_SUPPLY)], b)[0]
        have[d] = int.from_bytes(ret[:32], "big") / 10 ** token["decimals"]
        con.execute("INSERT OR REPLACE INTO supply_daily (token_symbol, date, supply) VALUES (?,?,?)",
                    (symbol, d, have[d]))
    con.commit()
    return have


def cohort_change(con, symbol: str, start: str, end: str, category: str = "whale") -> dict | None:
    """Point-in-time: members of `start`'s cohort, their holdings at start vs end."""
    rows = con.execute(
        """SELECT c.address, c.total, COALESCE(h.total, 0)
           FROM cohort_daily c
           LEFT JOIN holdings_daily h ON h.token_symbol = c.token_symbol AND h.address = c.address AND h.date = ?
           WHERE c.token_symbol = ? AND c.date = ? AND c.category = ?""",
        (end, symbol, start, category)).fetchall()
    if not rows:
        return None
    before, after = sum(r[1] for r in rows), sum(r[2] for r in rows)
    return {"n": len(rows), "pct": (after - before) / before if before else 0.0,
            "delta": after - before,
            "up": sum(1 for _, a, b in rows if b > a * (1 + _MOVE)),
            "down": sum(1 for _, a, b in rows if b < a * (1 - _MOVE))}


def export_token(con, symbol: str) -> dict | None:
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM cohort_daily WHERE token_symbol = ? ORDER BY date", (symbol,))]
    if not dates:
        return None
    supply = supply_series(con, symbol)
    latest = dates[-1]
    end = date.fromisoformat(latest)

    whale_tot = dict(con.execute(
        "SELECT date, SUM(total) FROM cohort_daily WHERE token_symbol = ? AND category = 'whale' GROUP BY date",
        (symbol,)))
    insider_tot = dict(con.execute(
        """SELECT h.date, SUM(h.total) FROM holdings_daily h
           JOIN universe u ON u.token_symbol = h.token_symbol AND u.address = h.address
           WHERE h.token_symbol = ? AND u.insider_reason IS NOT NULL AND u.excluded IS NULL
           GROUP BY h.date""", (symbol,)))
    series = [{"date": d, "whale_share": whale_tot.get(d, 0) / supply[d] if supply.get(d) else None,
               "insider_share": insider_tot.get(d, 0) / supply[d] if supply.get(d) else None}
              for d in dates]

    flows = []
    for i in range(len(dates) - 1, 6, -7):
        start, stop = dates[i - 7], dates[i]
        c = cohort_change(con, symbol, start, stop)
        if c:
            flows.append({"week_end": stop, "pct_supply": c["delta"] / supply[stop], "pct": c["pct"],
                          "up": c["up"], "down": c["down"]})
    flows.reverse()

    stats = {"whale_share": series[-1]["whale_share"], "insider_share": series[-1]["insider_share"],
             "supply": supply[latest]}
    for w in WINDOWS:
        start = (end - timedelta(days=w)).isoformat()
        stats[f"whales_{w}d"] = cohort_change(con, symbol, start, latest)
        stats[f"insiders_{w}d"] = cohort_change(con, symbol, start, latest, "insider")

    prev_30 = (end - timedelta(days=30)).isoformat()
    def d30(addr, now):
        """Fractional 30-day change; "new" when the wallet held nothing 30 days ago."""
        r = con.execute("SELECT total FROM holdings_daily WHERE token_symbol = ? AND address = ? AND date = ?",
                        (symbol, addr, prev_30)).fetchone()
        if r and r[0]:
            return (now - r[0]) / r[0]
        return "new" if r is not None else None

    whales = []
    for addr, rank, total in con.execute(
            "SELECT address, rank, total FROM cohort_daily WHERE token_symbol = ? AND date = ? AND category = 'whale' "
            "ORDER BY rank", (symbol, latest)):
        u = con.execute("SELECT wallet_type, labels, entity_id FROM universe WHERE token_symbol = ? AND address = ?",
                        (symbol, addr)).fetchone() or (None, "[]", None)
        h = con.execute("SELECT wallet_balance, staked_balance FROM holdings_daily WHERE token_symbol = ? "
                        "AND address = ? AND date = ?", (symbol, addr, latest)).fetchone() or (total, 0)
        # Contract-name tags like "GnosisSafeProxy_a8d4_7846" say nothing a reader needs
        labels = list(dict.fromkeys(l for l in json.loads(u[1] or "[]") if "safeproxy" not in l.lower()))
        whales.append({"address": addr, "rank": rank, "type": u[0], "total": total, "wallet": h[0],
                       "staked": h[1], "share": total / supply[latest], "d30": d30(addr, total),
                       "labels": labels[:2], "entity": u[2]})
    insiders = []
    for addr, reason, total in con.execute(
            """SELECT u.address, u.insider_reason, h.total FROM universe u
               JOIN holdings_daily h ON h.token_symbol = u.token_symbol AND h.address = u.address AND h.date = ?
               WHERE u.token_symbol = ? AND u.insider_reason IS NOT NULL AND u.excluded IS NULL AND h.total > 0
               ORDER BY h.total DESC""", (latest, symbol)):
        insiders.append({"address": addr, "total": total, "share": total / supply[latest],
                         "reason": reason, "d30": d30(addr, total)})

    notes = []
    sd = sorted(supply.items())
    for (d0, s0), (d1, s1) in zip(sd, sd[1:]):
        if s0 and abs(s1 - s0) / s0 > _SUPPLY_JUMP:
            notes.append(f"Total supply changed {(s1 - s0) / s0:+.1%} on {d1} "
                         f"({s0:,.0f} → {s1:,.0f}). Share-of-supply figures shift on that date.")
    q = con.execute("SELECT COUNT(*), SUM(complete) FROM cohort_quality WHERE token_symbol = ?", (symbol,)).fetchone()

    data = {"symbol": symbol, "contract": TOKEN_BASKET[symbol]["contract"], "as_of": latest,
            "stats": stats, "series": series, "flows": flows, "whales": whales, "insiders": insiders,
            "notes": notes, "quality": {"days": q[0], "proven": q[1] or 0}}
    (SITE / "data" / f"{symbol}.json").write_text(json.dumps(data, separators=(",", ":")))
    return {"symbol": symbol, "as_of": latest, "stats": stats,
            "spark": [s["whale_share"] for s in series[-90:]], "notes": len(notes),
            "quality": data["quality"]}


def export_methodology() -> None:
    body = markdown.markdown((ROOT / "docs" / "METHODOLOGY.md").read_text(), extensions=["tables"])
    template = (SITE / "methodology.template.html").read_text()
    (SITE / "methodology.html").write_text(template.replace("<!--BODY-->", body))


def main():
    init_db()
    (SITE / "data").mkdir(parents=True, exist_ok=True)
    con = get_connection()
    tokens, pending = [], []
    for symbol in TOKEN_BASKET:
        row = export_token(con, symbol)
        if row:
            tokens.append(row)
            print(f"  {symbol}: exported ({row['as_of']})")
        else:
            pending.append(symbol)
            print(f"  {symbol}: no point-in-time cohorts yet — listed as pending")
    con.close()
    (SITE / "data" / "overview.json").write_text(json.dumps(
        {"tokens": tokens, "pending": pending}, separators=(",", ":")))
    export_methodology()
    print(f"Wrote site/data for {len(tokens)} tokens ({len(pending)} pending)")


if __name__ == "__main__":
    main()
