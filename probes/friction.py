"""probes/friction.py — Probe B.

Question: can we see real bid/ask for free at pre-market latencies, and if
not, does a range/volume proxy track real spread well enough to substitute?
This module collects both, raw, side by side. It does NOT decide the answer
— that comparison happens later, off this data, by a human.

Two things get written each tick, per ticker:
  1. Whatever bid/ask the free sources will give up (currently: yfinance's
     info dict — Finnhub's free /quote has no bid/ask field at all, which is
     itself the finding, recorded via probes/sources.fetch_finnhub_quote's
     'no bid/ask' shape rather than silently skipped).
  2. A spread PROXY computed from the most recent completed daily bar's
     high-low range and volume — NOT from anything intraday, since a
     pre-market session's own high/low/volume are still forming and would
     conflate "proxy" with "the thing we're trying to predict."

Proxy formula (versioned — see spread_proxy_formula on each row):
  range_over_volume_v1:
      spread_proxy     = (day_high - day_low) / day_volume
      spread_proxy_pct = (day_high - day_low) / day_close * 100

  Rationale: a wide daily range on thin volume is the classic shape of an
  illiquid, wide-spread name; a wide range on heavy volume usually is not.
  Dividing range by volume is a simple, transparent way to fold both inputs
  into one number without pretending to model microstructure. It is a
  proxy, not a spread estimate — CLAUDE.md's "you never produce a number"
  rule binds the brief, not this exploratory probe, but the same spirit
  applies: this number is clearly labeled as a formula, not a fact, and its
  formula id travels with every row so it can be replaced without
  corrupting history.
"""

from __future__ import annotations

import logging
import sqlite3

from probes import db, sources

logger = logging.getLogger("premonition.probe.friction")

PROBE = "friction"
SPREAD_PROXY_FORMULA = "range_over_volume_v1"


def _with_spread_proxy(row: dict) -> dict:
    high, low, close, vol = row.get("day_high"), row.get("day_low"), row.get("day_close"), row.get("day_volume")
    if high is None or low is None:
        return row
    if close:
        row["spread_proxy_pct"] = (high - low) / close * 100
    if vol:
        row["spread_proxy"] = (high - low) / vol
    if row.get("spread_proxy") is not None or row.get("spread_proxy_pct") is not None:
        row["spread_proxy_formula"] = SPREAD_PROXY_FORMULA
    return row


def collect_tick(conn: sqlite3.Connection, tickers: list[str], finnhub_api_key: str | None) -> None:
    """One polling round: bid/ask sources plus the daily-bar proxy, every ticker, once."""
    for ticker in tickers:
        # Bid/ask — yfinance's info dict is the only free source that carries it at all.
        try:
            row = sources.fetch_yfinance_quote(PROBE, ticker)
        except Exception as e:  # noqa: BLE001
            logger.exception("unexpected exception from fetch_yfinance_quote(%s)", ticker)
            row = {
                "probe": PROBE, "source": "yfinance_quote", "ticker": ticker,
                "fetched_at": sources.now_iso(), "status": "error",
                "error": f"unhandled {type(e).__name__}: {e}", "raw_payload": "{}",
            }
        db.insert_observation(conn, row)
        if row.get("status") != "ok":
            logger.info("yfinance_quote %s: %s (%s)", ticker, row.get("status"), row.get("error"))
        elif row.get("bid") is None and row.get("ask") is None:
            logger.info("yfinance_quote %s: ok but no bid/ask in payload", ticker)

        # Finnhub free quote — recorded for completeness; its free tier has no bid/ask field.
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

        # Daily bar -> spread proxy.
        try:
            row = sources.fetch_yfinance_daily(PROBE, ticker)
        except Exception as e:  # noqa: BLE001
            logger.exception("unexpected exception from fetch_yfinance_daily(%s)", ticker)
            row = {
                "probe": PROBE, "source": "yfinance_daily", "ticker": ticker,
                "fetched_at": sources.now_iso(), "status": "error",
                "error": f"unhandled {type(e).__name__}: {e}", "raw_payload": "{}",
            }
        row = _with_spread_proxy(row)
        db.insert_observation(conn, row)
        if row.get("status") != "ok":
            logger.info("yfinance_daily %s: %s (%s)", ticker, row.get("status"), row.get("error"))

    conn.commit()
