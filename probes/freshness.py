"""probes/freshness.py — Probe A.

Question: can we see a fresh pre-market price and volume for free, at the
latencies this system needs? This module does not answer that question —
it only collects the raw evidence. One call per (ticker, source) per tick
for everything except Alpaca (batched across all tickers in one call each —
see probes/sources.py's module docstring for why), written straight to
probe.sqlite. No retries, no gap-filling, no summary statistics computed
here — quote_sanity results are the one exception: they're attached to
Alpaca's rows as recorded observations, never used to drop or alter a row,
since this probe's whole job is to show what a source actually returned,
garbage included.
"""

from __future__ import annotations

import logging
import sqlite3

from probes import db, quote_sanity, sources

logger = logging.getLogger("premonition.probe.freshness")

PROBE = "freshness"


def _apply_sanity(row: dict) -> dict:
    result = quote_sanity.check_quote_sanity(
        row.get("bid"), row.get("ask"), row.get("source_ts"), row.get("fetched_at"))
    row["quote_sanity_status"] = result["sanity_status"]
    row["quote_sanity_reason"] = result["sanity_reason"]
    row["quote_age_secs"] = result["quote_age_secs"]
    row["spread_width"] = result["spread_width"]
    row["quote_sanity_formula"] = result["quote_sanity_formula"]
    return row


def collect_tick(conn: sqlite3.Connection, tickers: list[str], finnhub_api_key: str | None,
                  alpaca_key_id: str | None = None, alpaca_secret_key: str | None = None) -> None:
    """One polling round: every source, every ticker, exactly once."""
    alpaca_quotes = sources.fetch_alpaca_quotes_batch(PROBE, tickers, alpaca_key_id, alpaca_secret_key)
    alpaca_bars = sources.fetch_alpaca_bars_1m_batch(PROBE, tickers, alpaca_key_id, alpaca_secret_key)

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

        alpaca_q_row = _apply_sanity(alpaca_quotes[ticker])
        db.insert_observation(conn, alpaca_q_row)
        if alpaca_q_row.get("status") == "ok" and alpaca_q_row.get("quote_sanity_status") != "ok":
            logger.info("alpaca_quote %s: sane-http-ok but sanity=%s (%s)", ticker,
                         alpaca_q_row.get("quote_sanity_status"), alpaca_q_row.get("quote_sanity_reason"))
        elif alpaca_q_row.get("status") != "ok":
            logger.info("alpaca_quote %s: %s (%s)", ticker, alpaca_q_row.get("status"), alpaca_q_row.get("error"))

        alpaca_b_row = alpaca_bars[ticker]
        db.insert_observation(conn, alpaca_b_row)
        if alpaca_b_row.get("status") != "ok":
            logger.info("alpaca_bars_1m %s: %s (%s)", ticker, alpaca_b_row.get("status"), alpaca_b_row.get("error"))

    conn.commit()
