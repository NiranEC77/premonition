"""seismo/collect_earnings.py — earnings dates from three independent sources.

DESIGN.md: "Cross-check. Disagreement is a flag, not something to average."
This writes one row per (ticker, source) — never a single merged date — so
that disagreement is visible to whatever reads earnings_dates later, rather
than silently resolved in this collector's favor of one source. FMP joined
yfinance and Finnhub as the third cross-check on 2026-07-16 — this is the
heaviest single feature in the scorer (catalyst freshness/type), and a wrong
date silently poisons it, which is exactly why a THIRD independent source is
worth the extra API calls: two sources agreeing could still both be wrong,
three agreeing is real signal.

FMP's IPO calendar endpoint (/stable/ipos-calendar) was also evaluated for
this collector, since DESIGN.md flags SPCX's IPO lockup expiry as one of the
largest scheduled catalysts on the whole watchlist — it returned HTTP 402
"Restricted Endpoint... upgrade your plan" on the current key's tier. Not
implemented; not worked around. The splits calendar (/stable/splits-calendar)
is NOT restricted and is wired in below.

Usage:
    python3 -m seismo.collect_earnings [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests
import yfinance as yf

from seismo import facts_db
from seismo.universe import resolved_tickers

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
FMP_EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings"
FMP_SPLITS_CALENDAR_URL = "https://financialmodelingprep.com/stable/splits-calendar"
HTTP_TIMEOUT_SECS = 10


def _yfinance_earnings_date(ticker: str) -> str | None:
    cal = yf.Ticker(ticker).calendar
    if not cal:
        return None
    dates = cal.get("Earnings Date")
    if not dates:
        return None
    return str(dates[0])  # nearest date when a range is given


def _finnhub_earnings_date(ticker: str, api_key: str | None) -> tuple[str | None, str | None]:
    """Returns (date, error). No key -> (None, 'missing FINNHUB_API_KEY'), an
    honest error, not a silent skip."""
    if not api_key:
        return None, "missing FINNHUB_API_KEY"
    try:
        resp = requests.get(
            FINNHUB_URL,
            params={"symbol": ticker, "token": api_key},
            timeout=HTTP_TIMEOUT_SECS,
        )
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {e}"

    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"

    data = resp.json().get("earningsCalendar") or []
    if not data:
        return None, None  # no upcoming earnings scheduled — a real result, not an error
    return data[0].get("date"), None


def _fmp_earnings_date(ticker: str, api_key: str | None) -> tuple[str | None, str | None]:
    """Returns (date, error). FMP's /stable/earnings returns the next
    scheduled date first, then historical dates in descending order — but
    that ordering is NOT contractually guaranteed, so this filters
    explicitly for date >= today rather than trusting data[0] blindly."""
    if not api_key:
        return None, "missing FMP_API_KEY"
    try:
        resp = requests.get(
            FMP_EARNINGS_URL,
            params={"symbol": ticker, "apikey": api_key},
            timeout=HTTP_TIMEOUT_SECS,
        )
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {e}"

    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"

    data = resp.json()
    if not isinstance(data, list) or not data:
        return None, None  # no data at all for this symbol — a real result, not an error

    today = date.today().isoformat()
    upcoming = [row.get("date") for row in data if row.get("date") and row["date"] >= today]
    if not upcoming:
        return None, None  # nothing upcoming scheduled — a real result, not an error
    return min(upcoming), None


def _fmp_splits_calendar(api_key: str | None, tickers: set[str], lookback_days: int = 7,
                          lookahead_days: int = 180) -> tuple[list[dict], str | None]:
    """Market-wide upcoming (and recently-passed) splits, filtered down to
    this watchlist. Returns (rows, error)."""
    if not api_key:
        return [], "missing FMP_API_KEY"
    frm = (date.today() - timedelta(days=lookback_days)).isoformat()
    to = (date.today() + timedelta(days=lookahead_days)).isoformat()
    try:
        resp = requests.get(
            FMP_SPLITS_CALENDAR_URL,
            params={"from": frm, "to": to, "apikey": api_key},
            timeout=HTTP_TIMEOUT_SECS,
        )
    except requests.RequestException as e:
        return [], f"{type(e).__name__}: {e}"

    if resp.status_code != 200:
        return [], f"HTTP {resp.status_code}"

    data = resp.json()
    if not isinstance(data, list):
        return [], None
    return [row for row in data if row.get("symbol") in tickers], None


def collect(conn, tickers: list[str], finnhub_api_key: str | None, fmp_api_key: str | None) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    ok, failed = 0, 0
    for ticker in tickers:
        try:
            yf_date = _yfinance_earnings_date(ticker)
        except Exception as e:  # noqa: BLE001
            print(f"  {ticker}: yfinance ERROR {type(e).__name__}: {e}")
            yf_date = None

        fh_date, fh_error = _finnhub_earnings_date(ticker, finnhub_api_key)
        fmp_date, fmp_error = _fmp_earnings_date(ticker, fmp_api_key)

        conn.execute(
            "INSERT OR REPLACE INTO earnings_dates (ticker, source, earnings_date, error, fetched_at) "
            "VALUES (?,?,?,?,?)",
            (ticker, "yfinance", yf_date, None, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO earnings_dates (ticker, source, earnings_date, error, fetched_at) "
            "VALUES (?,?,?,?,?)",
            (ticker, "finnhub", fh_date, fh_error, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO earnings_dates (ticker, source, earnings_date, error, fetched_at) "
            "VALUES (?,?,?,?,?)",
            (ticker, "fmp", fmp_date, fmp_error, now),
        )

        dates_seen = {d[:10] for d in (yf_date, fh_date, fmp_date) if d}
        flag = "  <-- DISAGREE" if len(dates_seen) > 1 else ""
        print(f"  {ticker}: yfinance={yf_date} finnhub={fh_date}{f' ({fh_error})' if fh_error else ''} "
              f"fmp={fmp_date}{f' ({fmp_error})' if fmp_error else ''}{flag}")
        ok += 1
        time.sleep(1.1)  # stay under Finnhub free tier's ~60/min limit rather than burst and 429

    conn.commit()

    splits, splits_error = _fmp_splits_calendar(fmp_api_key, set(tickers))
    if splits_error:
        print(f"\nFMP splits calendar: ERROR {splits_error}")
    else:
        for row in splits:
            headline = (f"Stock split: {row.get('numerator')}-for-{row.get('denominator')} "
                        f"effective {row.get('date')}")
            conn.execute(
                "INSERT INTO catalysts (ticker, headline, source, source_url, published_at, fetched_at) "
                "VALUES (?,?,?,?,?,?)",
                (row["symbol"], headline, "fmp_splits_calendar", None, row.get("date"), now),
            )
        conn.commit()
        print(f"\nFMP splits calendar: {len(splits)} upcoming split(s) on the watchlist"
              + (f" — {[r['symbol'] for r in splits]}" if splits else ""))

    return ok, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tickers = [t["ticker"] for t in resolved_tickers()]
    if args.dry_run:
        tickers = tickers[:3]

    finnhub_api_key = os.environ.get("FINNHUB_API_KEY")
    fmp_api_key = os.environ.get("FMP_API_KEY")
    print(f"collecting earnings dates for {len(tickers)} tickers")
    conn = facts_db.connect()
    ok, failed = collect(conn, tickers, finnhub_api_key, fmp_api_key)
    conn.close()
    print(f"done: {ok} ok, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
