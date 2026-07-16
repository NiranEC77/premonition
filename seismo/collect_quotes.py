"""seismo/collect_quotes.py — live quote snapshot at lock time, all resolved tickers.

Alpaca IEX is the PRIMARY source for premarket price/volume/bid-ask, as of
2026-07-16 — real market data, not Yahoo's free chart API, which reports
zero volume for every extended-hours bar structurally (see facts_db.py's
module docstring). yfinance is the fallback, used only when Alpaca has
nothing for a ticker. Both are batched-call adapters from probes/sources.py,
reused directly rather than re-derived — same rule as before: build data
plumbing once, use it everywhere.

Every quote that reaches facts.sqlite has passed probes/quote_sanity.py's
two gates — freshness (is the source's own timestamp actually recent?) and
spread sanity (is the bid/ask gap even plausible?) — before its price
becomes a number anyone can rank on. A quote that fails either gate is not
degraded and published anyway: price/bid/ask/premarket_* are left NULL (so
the tradability gate naturally treats the ticker as insufficient data — a
garbage quote IS the signal that a name is asleep, and an asleep name has no
business in the candidate pool) and the rejection is logged, in full, to
/srv/premonition/logs/quote-sanity-rejects.jsonl — same pattern as
CLAUDE.md's verify-rejects.jsonl: never published, never silently dropped
either.

Usage:
    python3 -m seismo.collect_quotes [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from probes import quote_sanity
from probes.sources import (
    fetch_alpaca_bars_1m_batch,
    fetch_alpaca_quotes_batch,
    fetch_yfinance_bars_1m,
    fetch_yfinance_quote,
)
from seismo import facts_db
from seismo.universe import resolved_tickers

ET = ZoneInfo("America/New_York")
REJECTS_LOG_PATH = "/srv/premonition/logs/quote-sanity-rejects.jsonl"


def _infer_market_state(now_et: datetime) -> str:
    """Computed from the ET wall clock, the same way regardless of which
    source answered — Alpaca doesn't self-report a market state at all, and
    relying on each source's own opinion (as the yfinance-only version of
    this module used to) is exactly the kind of cross-source inconsistency
    this project keeps tripping over. One clock, one answer."""
    t = now_et.time()
    if t >= datetime.strptime("04:00", "%H:%M").time() and t < datetime.strptime("09:30", "%H:%M").time():
        return "PRE"
    if t >= datetime.strptime("09:30", "%H:%M").time() and t < datetime.strptime("16:00", "%H:%M").time():
        return "REGULAR"
    if t >= datetime.strptime("16:00", "%H:%M").time() and t < datetime.strptime("20:00", "%H:%M").time():
        return "POST"
    return "CLOSED"


def _premarket_high_low_yfinance(ticker: str) -> tuple[float | None, float | None]:
    """Fallback only — used when Alpaca's bars had nothing for this ticker.
    Today's pre-market session high/low from completed 1-minute bars only
    (04:00-09:29:59 ET), same forming-bar exclusion rule as everywhere else
    in this codebase."""
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


def _latest_prev_close(conn, ticker: str) -> float | None:
    """Yesterday's actual close, from daily_bars — NOT today's regular_market_price,
    which is today's own (possibly still-forming) session, not the prior close."""
    row = conn.execute(
        "SELECT close FROM daily_bars WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return row[0] if row else None


def _log_reject(ticker: str, sanity: dict, bid: float | None, ask: float | None,
                 source: str, fetched_at: str) -> None:
    Path(REJECTS_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(REJECTS_LOG_PATH, "a") as f:
        f.write(json.dumps({
            "ticker": ticker, "fetched_at": fetched_at, "source": source,
            "bid": bid, "ask": ask, **sanity,
        }) + "\n")


DOLLAR_VOLUME_LOG_DIR = "/srv/premonition/logs"


def _log_dollar_volume_ranking(rows: list[tuple[str, float, float, str | None]], now_et: datetime) -> None:
    """Top 10 tickers by premarket price * premarket_volume, every run — not
    for ranking or gating, purely so tradability.yaml's provisional
    IEX-scaled floor can be replaced with a real, measured number instead of
    a guess. See tradability.yaml's 2026-07-16 note."""
    if not rows:
        return
    top10 = sorted(rows, key=lambda r: r[1] * r[2], reverse=True)[:10]
    log_path = Path(DOLLAR_VOLUME_LOG_DIR) / f"premarket-dollar-volume-{now_et.date().isoformat()}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(f"--- {now_et.isoformat()} ---\n")
        for ticker, price, vol, source in top10:
            f.write(f"{ticker}: price={price} premarket_volume={vol} "
                    f"dollar_volume={price * vol:,.0f} source={source}\n")
    print(f"\ntop 10 by premarket dollar volume (logged to {log_path}):")
    for ticker, price, vol, source in top10:
        print(f"  {ticker}: price={price} premarket_volume={vol} dollar_volume=${price * vol:,.0f} (source={source})")


def collect(conn, tickers: list[str]) -> tuple[int, int]:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    now_et = datetime.now(ET)
    market_state = _infer_market_state(now_et)
    ok, failed = 0, 0
    dollar_volume_rows: list[tuple[str, float, float, str | None]] = []

    alpaca_key_id = os.environ.get("ALPACA_API_KEY_ID")
    alpaca_secret_key = os.environ.get("ALPACA_API_SECRET_KEY")
    alpaca_quotes = fetch_alpaca_quotes_batch("lock", tickers, alpaca_key_id, alpaca_secret_key)
    alpaca_bars = fetch_alpaca_bars_1m_batch("lock", tickers, alpaca_key_id, alpaca_secret_key)

    for ticker in tickers:
        alpaca_q = alpaca_quotes.get(ticker, {})
        alpaca_b = alpaca_bars.get(ticker, {})

        # --- price / volume / high-low: Alpaca primary, yfinance fallback ---
        price_source = None
        price = premarket_volume = pm_vol_source = premarket_high = premarket_low = None

        if alpaca_b.get("status") == "ok" and alpaca_b.get("price") is not None:
            price = alpaca_b["price"]
            premarket_volume = alpaca_b.get("cumulative_volume_since_open")
            pm_vol_source = "alpaca_bar_sum" if premarket_volume is not None else None
            premarket_high = alpaca_b.get("session_high")
            premarket_low = alpaca_b.get("session_low")
            price_source = "alpaca"

        yf_quote_row = None
        if price_source is None:
            yf_quote_row = fetch_yfinance_quote("lock", ticker)
            if yf_quote_row.get("status") != "ok":
                print(f"  {ticker}: quote error, both alpaca and yfinance failed "
                      f"({yf_quote_row.get('error')})")
                failed += 1
                continue
            yf_bars_row = fetch_yfinance_bars_1m("lock", ticker)
            price = yf_quote_row.get("price")
            if yf_quote_row.get("premarket_volume") is not None:
                premarket_volume, pm_vol_source = yf_quote_row["premarket_volume"], "quote_field"
            elif yf_bars_row.get("status") == "ok" and yf_bars_row.get("cumulative_volume_since_open") is not None:
                premarket_volume, pm_vol_source = yf_bars_row["cumulative_volume_since_open"], "bar_sum_completed"
            premarket_high, premarket_low = _premarket_high_low_yfinance(ticker)
            price_source = "yfinance_quote"

        # --- bid/ask: Alpaca primary, yfinance fallback ---
        bid = ask = bid_source_ts = None
        if alpaca_q.get("status") == "ok" and (alpaca_q.get("bid") is not None or alpaca_q.get("ask") is not None):
            bid, ask, bid_source_ts = alpaca_q.get("bid"), alpaca_q.get("ask"), alpaca_q.get("source_ts")
        elif yf_quote_row is None:
            yf_quote_row = fetch_yfinance_quote("lock", ticker)
        if bid is None and ask is None and yf_quote_row is not None:
            bid, ask, bid_source_ts = yf_quote_row.get("bid"), yf_quote_row.get("ask"), yf_quote_row.get("source_ts")

        # --- the two gates. Not optional. ---
        sanity = quote_sanity.check_quote_sanity(bid, ask, bid_source_ts, now)
        if sanity["sanity_status"] in ("crossed", "stale", "implausible_spread"):
            _log_reject(ticker, sanity, bid, ask, price_source, now)
            print(f"  {ticker}: REJECTED by quote sanity ({sanity['sanity_status']}: "
                  f"{sanity['sanity_reason']}) — price/bid/ask/premarket_volume all nulled, "
                  f"not a candidate this run")
            price = premarket_volume = pm_vol_source = premarket_high = premarket_low = bid = ask = None

        prev_close = _latest_prev_close(conn, ticker)
        premarket_price = price if market_state == "PRE" else None
        premarket_gap_pct = None
        if premarket_price is not None and prev_close:
            premarket_gap_pct = (premarket_price - prev_close) / prev_close * 100

        conn.execute(
            "INSERT OR REPLACE INTO quotes "
            "(ticker, market_state, price, prev_close, premarket_price, premarket_gap_pct, "
            " premarket_high, premarket_low, premarket_volume, premarket_volume_source, bid, ask, "
            " spread_width, quote_sanity_status, quote_sanity_reason, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticker, market_state, price, prev_close,
             premarket_price, premarket_gap_pct, premarket_high, premarket_low,
             premarket_volume, pm_vol_source, bid, ask,
             sanity.get("spread_width"), sanity["sanity_status"], sanity["sanity_reason"],
             price_source, now),
        )

        # Only a GENUINE pre-market observation extends the real historical
        # baseline (see facts_db.py's premarket_volume_history docstring) —
        # never today's regular/post volume mislabeled as pre-market, and
        # never a sanity-rejected reading.
        if market_state == "PRE" and premarket_volume is not None:
            today = now_et.date().isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO premarket_volume_history (ticker, date, premarket_volume, source, fetched_at) "
                "VALUES (?,?,?,?,?)",
                (ticker, today, premarket_volume, pm_vol_source, now),
            )

        if price is not None and premarket_volume is not None:
            dollar_volume_rows.append((ticker, price, premarket_volume, pm_vol_source))

        ok += 1
        print(f"  {ticker}: price={price} state={market_state} source={price_source} "
              f"premarket_vol={premarket_volume} (source={pm_vol_source}) "
              f"sanity={sanity['sanity_status']}")

    conn.commit()
    _log_dollar_volume_ranking(dollar_volume_rows, now_et)
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
