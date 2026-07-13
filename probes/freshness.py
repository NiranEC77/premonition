"""probes/freshness.py — Probe A.

Question: can we see a fresh pre-market price and volume for free, at the
latencies this system needs? This module does not answer that question —
it only collects the raw evidence. One call per (ticker, source) per tick,
written straight to probe.sqlite. No retries, no gap-filling, no summary
statistics computed here.
"""

from __future__ import annotations

import logging
import sqlite3

from probes import db, sources

logger = logging.getLogger("premonition.probe.freshness")

PROBE = "freshness"


def collect_tick(conn: sqlite3.Connection, tickers: list[str], finnhub_api_key: str | None) -> None:
    """One polling round: every source, every ticker, exactly once."""
    for ticker in tickers:
        for fetch in (sources.fetch_yfinance_quote, sources.fetch_yfinance_bars_1m):
            try:
                row = fetch(PROBE, ticker)
            except Exception as e:  # noqa: BLE001 — a bug in the adapter must not kill the poll loop
                logger.exception("unexpected exception from %s(%s)", fetch.__name__, ticker)
                row = {
                    "probe": PROBE, "source": fetch.__name__, "ticker": ticker,
                    "fetched_at": sources.now_iso(), "status": "error",
                    "error": f"unhandled {type(e).__name__}: {e}", "raw_payload": "{}",
                }
            db.insert_observation(conn, row)
            if row.get("status") != "ok":
                logger.info("%s %s: %s (%s)", row.get("source"), ticker,
                            row.get("status"), row.get("error"))

        try:
            row = sources.fetch_finnhub_quote(PROBE, ticker, finnhub_api_key)
        except Exception as e:  # noqa: BLE001
            logger.exception("unexpected exception from fetch_finnhub_quote(%s)", ticker)
            row = {
                "probe": PROBE, "source": "finnhub_quote", "ticker": ticker,
                "fetched_at": sources.now_iso(), "status": "error",
                "error": f"unhandled {type(e).__name__}: {e}", "raw_payload": "{}",
            }
        db.insert_observation(conn, row)
        if row.get("status") == "rate_limited":
            db.log_rate_limit(conn, "finnhub_quote", ticker, row["fetched_at"],
                               row.get("http_status"), row.get("error", ""))
            logger.warning("finnhub rate limited on %s", ticker)
        elif row.get("status") != "ok":
            logger.info("finnhub_quote %s: %s (%s)", ticker, row.get("status"), row.get("error"))

    conn.commit()
