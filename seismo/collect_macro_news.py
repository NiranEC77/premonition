"""seismo/collect_macro_news.py — macro/geopolitical headlines likely to move
this specific watchlist, not generic market news.

Pulls Finnhub's general news feed (broad — hundreds of items across every
category) and keeps only what matches a deterministic keyword filter tied to
the watchlist's own clusters (DESIGN.md section 3d): Fed/rates, oil/energy,
semiconductors/export controls, crypto/BTC, and major geopolitical events.
Ranked by recency among matches — timestamp-driven, same rule as everywhere
else in this project (fresh over stale). No relevance scoring beyond the
keyword match itself; Hermes can do real ranking later. This is deterministic
on purpose, for the same reason the rest of the collectors are: a number (or
here, a headline) that reached the dashboard should be traceable to a fixed,
inspectable rule, not a model's judgment call nobody can audit.

This is a SNAPSHOT, not an accumulating history — the table is wiped and
reloaded every run. The published brief's own macro_headlines field is what
preserves "what she actually saw that morning" for the historical record;
this table is just today's working data, same relationship daily_bars/quotes
has to premarket_volume_history.

Usage:
    python3 -m seismo.collect_macro_news [--dry-run]
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import requests

from seismo import facts_db

FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/news"
HTTP_TIMEOUT_SECS = 10
TOP_N = 10

# Deterministic keyword filter, tied to DESIGN.md's cluster table — not a
# generic "is this important news" model, a fixed rule anyone can read and
# audit. Matched case-insensitively against headline + summary combined.
CATEGORY_KEYWORDS = {
    "fed_rates": [
        "federal reserve", " fed ", "fed rate", "fed chair", "fomc", "interest rate",
        "rate cut", "rate hike", "powell", "central bank", "monetary policy", "treasury yield",
    ],
    "oil_energy": [
        "crude oil", "opec", "wti", "brent crude", "oil price", "oil prices", "energy prices",
        "natural gas", "oil supply", "oil production", "oil output",
    ],
    "semis_export_controls": [
        "semiconductor", "chip export", "export control", "chip ban", "export ban",
        "china chip", "entity list", "tsmc", "chip war", "export restriction", "nvidia export",
    ],
    "crypto": [
        "bitcoin", "crypto", "ethereum", " btc ", "digital asset", "stablecoin", "sec crypto",
    ],
    "geopolitical": [
        "iran", "israel", "taiwan strait", "china taiwan", "sanctions", "military strike",
        "missile", "geopolitical", "invasion", "tariff", "trade war", "ceasefire", "airstrike",
    ],
}


def _matched_category(headline: str, summary: str) -> str | None:
    haystack = f" {headline.lower()} {summary.lower()} "
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in haystack:
                return category
    return None


def _fetch_general_news(api_key: str | None) -> tuple[list[dict], str | None]:
    if not api_key:
        return [], "missing FINNHUB_API_KEY"
    try:
        resp = requests.get(
            FINNHUB_NEWS_URL,
            params={"category": "general", "token": api_key},
            timeout=HTTP_TIMEOUT_SECS,
        )
    except requests.RequestException as e:
        return [], f"{type(e).__name__}: {e}"

    if resp.status_code != 200:
        return [], f"HTTP {resp.status_code}"

    try:
        data = resp.json()
    except ValueError as e:
        return [], f"invalid JSON: {e}"

    if not isinstance(data, list):
        return [], None
    return data, None


def collect(conn, finnhub_api_key: str | None) -> tuple[int, str | None]:
    now = datetime.now(timezone.utc).isoformat()
    articles, error = _fetch_general_news(finnhub_api_key)
    if error:
        return 0, error

    matched = []
    for item in articles:
        headline = item.get("headline") or ""
        if not headline:
            continue
        category = _matched_category(headline, item.get("summary") or "")
        if category is None:
            continue
        published_at = None
        if item.get("datetime"):
            published_at = datetime.fromtimestamp(item["datetime"], tz=timezone.utc).isoformat()
        matched.append({
            "headline": headline, "source": item.get("source") or "unknown",
            "source_url": item.get("url"), "published_at": published_at, "category": category,
        })

    # Freshest first — a real fact from Finnhub's own timestamp, not our fetch time.
    matched.sort(key=lambda r: r["published_at"] or "", reverse=True)
    top = matched[:TOP_N]

    conn.execute("DELETE FROM macro_headlines")
    for row in top:
        conn.execute(
            "INSERT INTO macro_headlines (headline, source, source_url, published_at, category, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (row["headline"], row["source"], row["source_url"], row["published_at"], row["category"], now),
        )
    conn.commit()
    return len(top), None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    finnhub_api_key = os.environ.get("FINNHUB_API_KEY")

    print("collecting macro/geopolitical headlines")
    conn = facts_db.connect()
    count, error = collect(conn, finnhub_api_key)
    conn.close()

    if error:
        print(f"ERROR: {error}")
        return 1
    print(f"done: {count} headline(s) matched and stored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
