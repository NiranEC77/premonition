"""seismo/collect_fundamentals.py — float and short interest, all resolved tickers.

Sparse by nature (DESIGN.md: "Sparse. Refresh weekly.") — small/recent-IPO
names often have nothing here at all. A ticker with no float data gets a row
with NULLs, not a zero, and downstream code must treat that as "unknown,"
never as "zero float."

Usage:
    python3 -m seismo.collect_fundamentals [--dry-run]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import yfinance as yf

from seismo import facts_db
from seismo.universe import resolved_tickers


def collect(conn, tickers: list[str]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    ok, failed = 0, 0
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).get_info()
        except Exception as e:  # noqa: BLE001
            print(f"  {ticker}: ERROR {type(e).__name__}: {e}")
            failed += 1
            continue

        if not info:
            print(f"  {ticker}: no_data")
            failed += 1
            continue

        company_name = info.get("longName") or info.get("shortName")
        float_shares = info.get("floatShares")
        shares_outstanding = info.get("sharesOutstanding")
        short_pct_float = info.get("shortPercentOfFloat")

        conn.execute(
            "INSERT OR REPLACE INTO fundamentals "
            "(ticker, company_name, float_shares, shares_outstanding, short_pct_float, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (ticker, company_name, float_shares, shares_outstanding, short_pct_float, "yfinance_info", now),
        )
        print(f"  {ticker}: float={float_shares} short_pct_float={short_pct_float}")
        ok += 1

    conn.commit()
    return ok, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tickers = [t["ticker"] for t in resolved_tickers()]
    if args.dry_run:
        tickers = tickers[:3]

    print(f"collecting fundamentals for {len(tickers)} tickers")
    conn = facts_db.connect()
    ok, failed = collect(conn, tickers)
    conn.close()
    print(f"done: {ok} ok, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
