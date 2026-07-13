"""probes/sources.py — free-source adapters for the latency/friction probes.

Every function here makes ONE network call for ONE ticker and returns a dict
shaped for probes/db.py's `insert_observation`. None of them raise past their
own boundary: a network error, a bad response, a missing API key, or a
rate limit is a *result*, recorded honestly with status/error, never a crash
that would silently truncate the polling window.

Sources implemented:
  - yfinance_quote     — yf.Ticker(t).get_info(). The richest free source:
                         when the market is in a pre/post state, Yahoo's
                         quoteSummary payload (which this wraps) carries
                         preMarketPrice / preMarketTime / marketState, plus
                         bid/ask for the friction probe. Whether it actually
                         populates those fields before 09:30 is exactly the
                         open question Probe A exists to answer — this
                         adapter does not assume the answer, it just reads
                         whatever keys are present and records the rest of
                         the payload raw either way.
  - yfinance_bars_1m   — yf.Ticker(t).history(interval='1m', prepost=True).
                         A different code path through yfinance (the chart
                         API, not quoteSummary) that may have different
                         latency/availability characteristics. Volume here
                         is a SINGLE BAR's volume, not a cumulative session
                         total — labeled as such via volume_field.
  - yfinance_daily     — the most recent completed daily OHLCV bar. Used
                         only by the friction probe, to compute the
                         high-low/volume spread proxy. "Delayed is fine"
                         for this one per DESIGN.md.
  - finnhub_quote      — GET /api/v1/quote. Free tier: last price fields
                         (c/h/l/o/pc) and a quote timestamp (t). No bid/ask,
                         no volume, on the free tier — recorded as absent,
                         never defaulted to 0.

Other free sources considered and rejected for this phase:
  - Yahoo's raw v7/finance/quote HTTP endpoint: returns 401 without a
    session cookie + crumb, which yfinance obtains internally but which is
    not worth reimplementing when yfinance already exercises that same
    backend for us.
  - Stooq's CSV quote endpoint: the documented URL shape (`/q/l/?s=...`)
    404s as of 2026-07; not pursued further.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import requests
import yfinance as yf

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
HTTP_TIMEOUT_SECS = 10


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_now_iso = now_iso  # internal alias, kept short at call sites within this module


def _epoch_to_iso(epoch: Any) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _safe_json(obj: Any) -> str:
    """Best-effort JSON dump of a raw payload. Never raises — an
    unserializable payload becomes a string repr rather than losing the row."""
    try:
        return json.dumps(obj, default=str)
    except (TypeError, ValueError):
        return json.dumps({"unserializable_repr": repr(obj)})


def _base_row(probe: str, source: str, ticker: str) -> dict:
    return {
        "probe": probe,
        "source": source,
        "ticker": ticker,
        "fetched_at": _now_iso(),
        "status": "error",
        "raw_payload": "{}",
    }


# ---------------------------------------------------------------------------
# yfinance: quoteSummary-backed info dict
# ---------------------------------------------------------------------------

def fetch_yfinance_quote(probe: str, ticker: str) -> dict:
    row = _base_row(probe, "yfinance_quote", ticker)
    try:
        info = yf.Ticker(ticker).get_info()
    except Exception as e:  # noqa: BLE001 — any failure here is a recorded result, not a crash
        row["error"] = f"{type(e).__name__}: {e}"
        return row

    if not info:
        row["status"] = "no_data"
        return row

    row["raw_payload"] = _safe_json(info)
    row["market_state"] = info.get("marketState")

    pre_price = info.get("preMarketPrice")
    if pre_price is not None:
        row["price"] = pre_price
        row["source_ts_raw"] = info.get("preMarketTime")
        row["source_ts"] = _epoch_to_iso(info.get("preMarketTime"))
    else:
        row["price"] = info.get("regularMarketPrice")
        row["source_ts_raw"] = info.get("regularMarketTime")
        row["source_ts"] = _epoch_to_iso(info.get("regularMarketTime"))

    for vol_field in ("preMarketVolume", "regularMarketVolume", "volume"):
        if info.get(vol_field) is not None:
            row["volume"] = info.get(vol_field)
            row["volume_field"] = vol_field
            break

    row["bid"] = info.get("bid")
    row["ask"] = info.get("ask")
    row["bid_size"] = info.get("bidSize")
    row["ask_size"] = info.get("askSize")
    row["status"] = "ok" if (row["price"] is not None) else "no_data"
    return row


# ---------------------------------------------------------------------------
# yfinance: 1-minute chart bars, prepost included
# ---------------------------------------------------------------------------

def fetch_yfinance_bars_1m(probe: str, ticker: str) -> dict:
    row = _base_row(probe, "yfinance_bars_1m", ticker)
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
    except Exception as e:  # noqa: BLE001
        row["error"] = f"{type(e).__name__}: {e}"
        return row

    if hist is None or hist.empty:
        row["status"] = "no_data"
        return row

    last = hist.iloc[-1]
    last_ts = hist.index[-1]
    payload = {
        "timestamp": str(last_ts),
        "open": float(last["Open"]) if last["Open"] == last["Open"] else None,
        "high": float(last["High"]) if last["High"] == last["High"] else None,
        "low": float(last["Low"]) if last["Low"] == last["Low"] else None,
        "close": float(last["Close"]) if last["Close"] == last["Close"] else None,
        "volume": float(last["Volume"]) if last["Volume"] == last["Volume"] else None,
    }
    row["raw_payload"] = _safe_json(payload)
    row["source_ts_raw"] = str(last_ts)
    try:
        row["source_ts"] = last_ts.tz_convert("UTC").isoformat()
    except Exception:  # noqa: BLE001
        row["source_ts"] = None
    row["price"] = payload["close"]
    if payload["volume"] is not None:
        row["volume"] = payload["volume"]
        row["volume_field"] = "bar_volume_1m"  # NOT a cumulative session total
    row["status"] = "ok" if payload["close"] is not None else "no_data"
    return row


# ---------------------------------------------------------------------------
# yfinance: most recent completed daily bar (friction probe's proxy input)
# ---------------------------------------------------------------------------

def fetch_yfinance_daily(probe: str, ticker: str) -> dict:
    row = _base_row(probe, "yfinance_daily", ticker)
    try:
        hist = yf.Ticker(ticker).history(period="5d", interval="1d")
    except Exception as e:  # noqa: BLE001
        row["error"] = f"{type(e).__name__}: {e}"
        return row

    if hist is None or hist.empty:
        row["status"] = "no_data"
        return row

    last = hist.iloc[-1]
    last_ts = hist.index[-1]
    payload = {
        "date": str(last_ts.date()),
        "open": float(last["Open"]) if last["Open"] == last["Open"] else None,
        "high": float(last["High"]) if last["High"] == last["High"] else None,
        "low": float(last["Low"]) if last["Low"] == last["Low"] else None,
        "close": float(last["Close"]) if last["Close"] == last["Close"] else None,
        "volume": float(last["Volume"]) if last["Volume"] == last["Volume"] else None,
    }
    row["raw_payload"] = _safe_json(payload)
    row["day_bar_date"] = payload["date"]
    row["day_high"] = payload["high"]
    row["day_low"] = payload["low"]
    row["day_close"] = payload["close"]
    row["day_volume"] = payload["volume"]
    row["source_ts_raw"] = payload["date"]
    row["source_ts"] = payload["date"]
    row["status"] = "ok" if payload["high"] is not None else "no_data"
    return row


# ---------------------------------------------------------------------------
# Finnhub free quote endpoint
# ---------------------------------------------------------------------------

def fetch_finnhub_quote(probe: str, ticker: str, api_key: str | None) -> dict:
    row = _base_row(probe, "finnhub_quote", ticker)

    if not api_key:
        row["error"] = "missing FINNHUB_API_KEY"
        return row

    try:
        resp = requests.get(
            FINNHUB_QUOTE_URL,
            params={"symbol": ticker, "token": api_key},
            timeout=HTTP_TIMEOUT_SECS,
        )
    except requests.RequestException as e:
        row["error"] = f"{type(e).__name__}: {e}"
        return row

    row["http_status"] = resp.status_code

    if resp.status_code == 429:
        row["status"] = "rate_limited"
        row["error"] = "HTTP 429 from Finnhub"
        row["raw_payload"] = _safe_json({"text": resp.text[:2000]})
        return row

    if resp.status_code != 200:
        row["error"] = f"HTTP {resp.status_code}"
        row["raw_payload"] = _safe_json({"text": resp.text[:2000]})
        return row

    try:
        data = resp.json()
    except ValueError as e:
        row["error"] = f"invalid JSON: {e}"
        row["raw_payload"] = _safe_json({"text": resp.text[:2000]})
        return row

    row["raw_payload"] = _safe_json(data)

    # Finnhub returns all-zero fields for an unresolved/unknown symbol rather
    # than an error status — that is itself a "no_data" result, not a real
    # zero price.
    price = data.get("c")
    if not price:
        row["status"] = "no_data"
        row["source_ts_raw"] = data.get("t")
        row["source_ts"] = _epoch_to_iso(data.get("t"))
        return row

    row["price"] = price
    row["source_ts_raw"] = data.get("t")
    row["source_ts"] = _epoch_to_iso(data.get("t"))
    row["status"] = "ok"
    return row
