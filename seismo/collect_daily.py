"""seismo/collect_daily.py — daily OHLCV + ATR/typical-gap stats, all resolved tickers.

"Delayed is fine" for this one (DESIGN.md) — daily bars are for gap history
and volatility normalization, not the live lock. Run this at prep/draft time,
not repeatedly through the morning.

Usage:
    python3 -m seismo.collect_daily [--dry-run] [--days 90]
"""

from __future__ import annotations

import argparse
import statistics
from datetime import datetime, timezone

import yfinance as yf

from seismo import facts_db
from seismo.universe import resolved_tickers

STATS_FORMULA = "atr14_gap_pct_v1"


def _fetch_daily(ticker: str, days: int):
    hist = yf.Ticker(ticker).history(period=f"{days}d", interval="1d")
    return hist


def collect(conn, tickers: list[str], days: int) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    ok, failed = 0, 0
    for ticker in tickers:
        try:
            hist = _fetch_daily(ticker, days)
        except Exception as e:  # noqa: BLE001
            print(f"  {ticker}: ERROR {type(e).__name__}: {e}")
            failed += 1
            continue

        if hist is None or hist.empty:
            print(f"  {ticker}: no_data")
            failed += 1
            continue

        rows = []
        for ts, row in hist.iterrows():
            date = str(ts.date())
            rows.append((
                ticker, date,
                float(row["Open"]) if row["Open"] == row["Open"] else None,
                float(row["High"]) if row["High"] == row["High"] else None,
                float(row["Low"]) if row["Low"] == row["Low"] else None,
                float(row["Close"]) if row["Close"] == row["Close"] else None,
                float(row["Volume"]) if row["Volume"] == row["Volume"] else None,
                "yfinance_daily", now,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO daily_bars "
            "(ticker, date, open, high, low, close, volume, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )

        # ATR14, typical gap %, avg $ volume — computed here from what we just
        # stored, not fetched separately. NaN-safe: fewer than 15 bars (needed
        # for a 14-period true range) means atr14 stays NULL, not a guess.
        closes = [r[5] for r in rows if r[5] is not None]
        opens = [r[2] for r in rows if r[2] is not None]
        highs = [r[3] for r in rows if r[3] is not None]
        lows = [r[4] for r in rows if r[4] is not None]
        vols = [r[6] for r in rows if r[6] is not None]

        atr14 = None
        if len(rows) >= 15:
            true_ranges = []
            for i in range(1, len(rows)):
                h, l, prev_c = rows[i][3], rows[i][4], rows[i - 1][5]
                if None in (h, l, prev_c):
                    continue
                true_ranges.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
            if len(true_ranges) >= 14:
                atr14 = statistics.mean(true_ranges[-14:])

        typical_gap_pct = None
        gaps = []
        for i in range(1, len(rows)):
            o, prev_c = rows[i][2], rows[i - 1][5]
            if o is None or not prev_c:
                continue
            gaps.append(abs(o / prev_c - 1) * 100)
        if gaps:
            typical_gap_pct = statistics.mean(gaps)

        avg_dollar_vol20 = None
        dollar_vols = [c * v for c, v in zip(closes[-20:], vols[-20:]) if c is not None and v is not None]
        if dollar_vols:
            avg_dollar_vol20 = statistics.mean(dollar_vols)

        conn.execute(
            "INSERT OR REPLACE INTO daily_stats "
            "(ticker, atr14, avg_dollar_vol20, typical_gap_pct, sample_days, stats_formula, computed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (ticker, atr14, avg_dollar_vol20, typical_gap_pct, len(rows), STATS_FORMULA, now),
        )
        print(f"  {ticker}: {len(rows)} bars, atr14={atr14}, typical_gap_pct={typical_gap_pct}")
        ok += 1

    conn.commit()
    return ok, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tickers = [t["ticker"] for t in resolved_tickers()]
    print(f"collecting daily bars for {len(tickers)} tickers, {args.days}d lookback")

    if args.dry_run:
        tickers = tickers[:3]
        print(f"dry-run: limiting to {tickers}")

    conn = facts_db.connect()
    ok, failed = collect(conn, tickers, args.days)
    conn.close()
    print(f"done: {ok} ok, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
