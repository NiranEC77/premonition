"""seismo/collect_catalysts.py — timestamped news headlines, all resolved tickers.

Finnhub company-news is the primary source (real headlines, timestamps,
source URLs, confirmed working tonight with a real key). yfinance's .news is
the fallback when Finnhub has nothing for a ticker or the key is absent. The
headline TEXT itself is the fact here — this collector never summarizes or
rewrites it; whatever prose ends up in a pick card's "why" sentence downstream
must trace back to one of these rows, verbatim enough to check.

Usage:
    python3 -m seismo.collect_catalysts [--dry-run] [--lookback-days 3]
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf

from seismo import facts_db
from seismo.universe import resolved_tickers

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
HTTP_TIMEOUT_SECS = 10
MAX_HEADLINES_PER_TICKER = 5


def _finnhub_news(ticker: str, api_key: str, lookback_days: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    frm = (today - timedelta(days=lookback_days)).isoformat()
    resp = requests.get(
        FINNHUB_URL,
        params={"symbol": ticker, "from": frm, "to": today.isoformat(), "token": api_key},
        timeout=HTTP_TIMEOUT_SECS,
    )
    resp.raise_for_status()
    items = resp.json() or []
    out = []
    for item in items[:MAX_HEADLINES_PER_TICKER]:
        headline = item.get("headline")
        if not headline:
            continue
        published_at = None
        if item.get("datetime"):
            published_at = datetime.fromtimestamp(item["datetime"], tz=timezone.utc).isoformat()
        out.append({
            "headline": headline,
            "source": "finnhub_company_news",
            "source_url": item.get("url"),
            "published_at": published_at,
        })
    return out


def _yfinance_news(ticker: str) -> list[dict]:
    items = yf.Ticker(ticker).news or []
    out = []
    for item in items[:MAX_HEADLINES_PER_TICKER]:
        content = item.get("content", item)  # yfinance has changed this shape across versions
        headline = content.get("title") or item.get("title")
        if not headline:
            continue
        link = (content.get("canonicalUrl") or {}).get("url") or item.get("link")
        pub = content.get("pubDate") or item.get("providerPublishTime")
        published_at = None
        if isinstance(pub, str):
            published_at = pub
        elif isinstance(pub, (int, float)):
            published_at = datetime.fromtimestamp(pub, tz=timezone.utc).isoformat()
        out.append({
            "headline": headline,
            "source": "yfinance_news",
            "source_url": link,
            "published_at": published_at,
        })
    return out


def collect(conn, tickers: list[str], finnhub_api_key: str | None, lookback_days: int) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    ok, failed = 0, 0
    for ticker in tickers:
        headlines: list[dict] = []
        if finnhub_api_key:
            try:
                headlines = _finnhub_news(ticker, finnhub_api_key, lookback_days)
            except Exception as e:  # noqa: BLE001
                print(f"  {ticker}: finnhub ERROR {type(e).__name__}: {e}")
            time.sleep(1.1)  # stay under Finnhub free tier's ~60/min limit

        if not headlines:
            try:
                headlines = _yfinance_news(ticker)
            except Exception as e:  # noqa: BLE001
                print(f"  {ticker}: yfinance news ERROR {type(e).__name__}: {e}")

        for h in headlines:
            conn.execute(
                "INSERT INTO catalysts (ticker, headline, source, source_url, published_at, fetched_at) "
                "VALUES (?,?,?,?,?,?)",
                (ticker, h["headline"], h["source"], h["source_url"], h["published_at"], now),
            )

        print(f"  {ticker}: {len(headlines)} headline(s)"
              + (f" — {headlines[0]['headline'][:70]!r}" if headlines else ""))
        if headlines:
            ok += 1
        else:
            failed += 1  # 'failed' here means "no catalyst found," not a request error — still counted, never hidden

    conn.commit()
    return ok, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tickers = [t["ticker"] for t in resolved_tickers()]
    if args.dry_run:
        tickers = tickers[:3]

    finnhub_api_key = os.environ.get("FINNHUB_API_KEY")
    print(f"collecting catalysts for {len(tickers)} tickers (lookback {args.lookback_days}d)")
    conn = facts_db.connect()
    ok, failed = collect(conn, tickers, finnhub_api_key, args.lookback_days)
    conn.close()
    print(f"done: {ok} with headlines, {failed} with none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
