"""seismo/collect_macro.py — overnight world signals: crypto + index futures.

DESIGN.md: "BTC/ETH overnight — free crypto API. Trades 24/7. Predicts 9
names. Highest value-per-line-of-code in the repo." Also the futures/index
context that leads the semis complex (TSM in Taipei, Nikkei, DAX) and the
dollar (DXY).

Usage:
    python3 -m seismo.collect_macro [--dry-run]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import yfinance as yf

from seismo import facts_db

SYMBOLS = [
    ("BTC-USD", "Bitcoin"),
    ("ETH-USD", "Ethereum"),
    ("ES=F", "S&P 500 futures"),
    ("NQ=F", "Nasdaq 100 futures"),
    ("^N225", "Nikkei 225"),
    ("^TWII", "Taiwan Weighted (TSM leads semis)"),
    ("^GDAXI", "DAX"),
    ("DX-Y.NYB", "US Dollar Index"),
]


def collect(conn, symbols: list[tuple[str, str]]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    ok, failed = 0, 0
    for symbol, label in symbols:
        try:
            info = yf.Ticker(symbol).get_info()
        except Exception as e:  # noqa: BLE001
            print(f"  {symbol}: ERROR {type(e).__name__}: {e}")
            failed += 1
            continue

        price = info.get("regularMarketPrice") if info else None
        prev_close = info.get("regularMarketPreviousClose") if info else None
        change_pct = None
        if price is not None and prev_close:
            change_pct = (price - prev_close) / prev_close * 100

        if price is None:
            print(f"  {symbol}: no_data")
            failed += 1
            continue

        conn.execute(
            "INSERT OR REPLACE INTO macro_quotes (symbol, label, price, prev_close, change_pct, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol, label, price, prev_close, change_pct, "yfinance_info", now),
        )
        print(f"  {symbol} ({label}): {price} ({change_pct:+.2f}%)" if change_pct is not None
              else f"  {symbol} ({label}): {price} (no prev_close)")
        ok += 1

    conn.commit()
    return ok, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    symbols = SYMBOLS[:2] if args.dry_run else SYMBOLS
    print(f"collecting macro quotes for {len(symbols)} symbols")
    conn = facts_db.connect()
    ok, failed = collect(conn, symbols)
    conn.close()
    print(f"done: {ok} ok, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
