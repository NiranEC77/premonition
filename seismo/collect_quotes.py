"""seismo/collect_quotes.py — live quote snapshot at lock time, all resolved tickers.

Reuses probes/sources.py's adapters directly rather than re-deriving them —
those were built and validated in Phase 1/1c specifically for this. Two
independent attempts are made at pre-market volume, and BOTH are recorded
honestly:
  1. The quoteSummary `preMarketVolume` field (fetch_yfinance_quote).
  2. Summing completed 1-minute bars since the pre-market open (fetch_yfinance_bars_1m).

As of tonight (2026-07-13), method 2 is known to return 0 for every
extended-hours bar — see facts_db.py's module docstring. It is still
attempted and recorded rather than skipped, because that zero is itself
evidence, and because the finding could be wrong on a future run (a data
provider quirk fixed, a different session behaving differently) and this
collector should not assume its own prior finding is permanent. Method 1's
behavior during a REAL pre-market state is unknown until it is observed live.

Usage:
    python3 -m seismo.collect_quotes [--dry-run]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from probes.sources import fetch_yfinance_bars_1m, fetch_yfinance_quote
from seismo import facts_db
from seismo.universe import resolved_tickers


def _premarket_high_low(ticker: str) -> tuple[float | None, float | None]:
    """Today's pre-market session high/low so far, from completed 1-minute
    bars only (04:00-09:29:59 ET). The forming bar is excluded the same way
    probes/sources.py's fetch_yfinance_bars_1m excludes it: a bar counts as
    closed only once its bucket end (start + 1m) is at or before now. Volume
    in this window is known to be unreliable (see facts_db.py's docstring);
    price is not, so this is a legitimate, separate use of the same bar data."""
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
    except Exception:  # noqa: BLE001
        return None, None
    if hist is None or hist.empty:
        return None, None

    now = pd.Timestamp.now(tz=hist.index.tz) if hist.index.tz else pd.Timestamp.now()
    is_forming = (hist.index[-1] + pd.Timedelta(minutes=1)) > now
    completed = hist.iloc[:-1] if is_forming else hist

    window = completed.between_time("04:00", "09:29")
    if window.empty:
        return None, None
    return float(window["High"].max()), float(window["Low"].min())


def _resolve_premarket_volume(quote_row: dict, bars_row: dict) -> tuple[float | None, str | None]:
    """Prefer the quote field if present; record the bar-sum only if the quote
    field is absent, so provenance always shows which method actually worked."""
    if quote_row.get("status") == "ok" and quote_row.get("premarket_volume") is not None:
        return quote_row["premarket_volume"], "quote_field"
    if bars_row.get("status") == "ok" and bars_row.get("cumulative_volume_since_open") is not None:
        # Only meaningful pre-market if the source's own market_state says PRE —
        # otherwise this is regular/post-session volume, not pre-market.
        if quote_row.get("market_state") == "PRE":
            return bars_row["cumulative_volume_since_open"], "bar_sum_completed"
    return None, None


def _latest_prev_close(conn, ticker: str) -> float | None:
    """Yesterday's actual close, from daily_bars — NOT today's regular_market_price,
    which is today's own (possibly still-forming) session, not the prior close."""
    row = conn.execute(
        "SELECT close FROM daily_bars WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return row[0] if row else None


def collect(conn, tickers: list[str]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    ok, failed = 0, 0
    for ticker in tickers:
        quote_row = fetch_yfinance_quote("lock", ticker)
        bars_row = fetch_yfinance_bars_1m("lock", ticker)

        if quote_row.get("status") != "ok":
            print(f"  {ticker}: quote {quote_row.get('status')} ({quote_row.get('error')})")
            failed += 1
            continue

        premarket_volume, pm_vol_source = _resolve_premarket_volume(quote_row, bars_row)
        premarket_high, premarket_low = _premarket_high_low(ticker)

        prev_close = _latest_prev_close(conn, ticker)
        premarket_price = quote_row.get("premarket_price")
        premarket_gap_pct = None
        if premarket_price is not None and prev_close:
            premarket_gap_pct = (premarket_price - prev_close) / prev_close * 100

        conn.execute(
            "INSERT OR REPLACE INTO quotes "
            "(ticker, market_state, price, prev_close, premarket_price, premarket_gap_pct, "
            " premarket_high, premarket_low, premarket_volume, premarket_volume_source, bid, ask, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticker, quote_row.get("market_state"), quote_row.get("price"), prev_close,
             premarket_price, premarket_gap_pct, premarket_high, premarket_low,
             premarket_volume, pm_vol_source,
             quote_row.get("bid"), quote_row.get("ask"), "yfinance_quote", now),
        )

        # Only a GENUINE pre-market observation extends the real historical
        # baseline (see facts_db.py's premarket_volume_history docstring) —
        # never today's regular/post volume mislabeled as pre-market.
        if quote_row.get("market_state") == "PRE" and premarket_volume is not None:
            today = datetime.now(timezone.utc).astimezone().date().isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO premarket_volume_history (ticker, date, premarket_volume, source, fetched_at) "
                "VALUES (?,?,?,?,?)",
                (ticker, today, premarket_volume, pm_vol_source, now),
            )

        ok += 1
        print(f"  {ticker}: price={quote_row.get('price')} state={quote_row.get('market_state')} "
              f"premarket_vol={premarket_volume} (source={pm_vol_source})")

    conn.commit()
    return ok, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tickers = [t["ticker"] for t in resolved_tickers()]
    if args.dry_run:
        tickers = tickers[:3]

    print(f"collecting live quotes for {len(tickers)} tickers")
    conn = facts_db.connect()
    ok, failed = collect(conn, tickers)
    conn.close()
    print(f"done: {ok} ok, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
