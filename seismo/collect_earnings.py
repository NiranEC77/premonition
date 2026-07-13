"""seismo/collect_earnings.py — earnings dates from two independent sources.

DESIGN.md: "Cross-check. Disagreement is a flag, not something to average."
This writes one row per (ticker, source) — never a single merged date — so
that disagreement is visible to whatever reads earnings_dates later, rather
than silently resolved in this collector's favor of one source.

Usage:
    python3 -m seismo.collect_earnings [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

import requests
import yfinance as yf

from seismo import facts_db
from seismo.universe import resolved_tickers

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
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


def collect(conn, tickers: list[str], finnhub_api_key: str | None) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    ok, failed = 0, 0
    for ticker in tickers:
        try:
            yf_date = _yfinance_earnings_date(ticker)
        except Exception as e:  # noqa: BLE001
            print(f"  {ticker}: yfinance ERROR {type(e).__name__}: {e}")
            yf_date = None

        fh_date, fh_error = _finnhub_earnings_date(ticker, finnhub_api_key)

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

        flag = ""
        if yf_date and fh_date and yf_date[:10] != fh_date[:10]:
            flag = "  <-- DISAGREE"
        print(f"  {ticker}: yfinance={yf_date} finnhub={fh_date}{f' ({fh_error})' if fh_error else ''}{flag}")
        ok += 1
        time.sleep(1.1)  # stay under Finnhub free tier's ~60/min limit rather than burst and 429

    conn.commit()
    return ok, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tickers = [t["ticker"] for t in resolved_tickers()]
    if args.dry_run:
        tickers = tickers[:3]

    finnhub_api_key = os.environ.get("FINNHUB_API_KEY")
    print(f"collecting earnings dates for {len(tickers)} tickers")
    conn = facts_db.connect()
    ok, failed = collect(conn, tickers, finnhub_api_key)
    conn.close()
    print(f"done: {ok} ok, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
