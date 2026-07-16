"""probes/sources.py — free-source adapters for the latency/friction probes.

Every function here makes ONE network call for ONE ticker and returns a dict
shaped for probes/db.py's `insert_observation`. None of them raise past their
own boundary: a network error, a bad response, a missing API key, or a
rate limit is a *result*, recorded honestly with status/error, never a crash
that would silently truncate the polling window.

Sources implemented:
  - yfinance_quote     — yf.Ticker(t).get_info(). The richest free source:
                         Yahoo's quoteSummary payload (which this wraps)
                         carries regularMarketPrice/Volume/Time AND
                         preMarketPrice/Volume/Time AND
                         postMarketPrice/Volume/Time as separate fields, plus
                         bid/ask for the friction probe. All nine are read
                         and stored uncascaded, every tick, regardless of
                         marketState — whether regularMarket* actually
                         freezes once the regular session ends (as opposed
                         to continuing to update, which would make it
                         indistinguishable from a live pre-market feed and
                         a real risk of publishing a stale number) is
                         exactly what this adapter's columns exist to prove.
  - yfinance_bars_1m   — yf.Ticker(t).history(interval='1m', prepost=True).
                         A different code path through yfinance (the chart
                         API, not quoteSummary). The bar for the current,
                         still-open minute reports a partial volume (reads
                         as low or zero right after the minute turns over)
                         — this adapter never reports that as "the" volume.
                         It separates the last CLOSED bar's volume from the
                         still-forming bar's, and additionally sums every
                         closed bar back to the window's start (04:00 ET,
                         empirically) into a cumulative total.
  - yfinance_daily     — the most recent completed daily OHLCV bar. Used
                         only by the friction probe, to compute the
                         high-low/volume spread proxy. "Delayed is fine"
                         for this one per DESIGN.md.
  - finnhub_quote      — GET /api/v1/quote. Free tier: last price fields
                         (c/h/l/o/pc) and a quote timestamp (t). No bid/ask,
                         no volume, on the free tier — recorded as absent,
                         never defaulted to 0.
  - alpaca_quote       — GET /v2/stocks/quotes/latest, ALL tickers in ONE
                         call (Alpaca's multi-symbol batch endpoint) rather
                         than one call per ticker like everything else here —
                         documented deviation, not an oversight; at ~90
                         tickers, one-call-per-ticker would be both slow and
                         needlessly close to rate limits. Free tier is the
                         IEX feed only (~2% of consolidated volume, per
                         DESIGN.md's open question about thin names). Every
                         row carries Alpaca's OWN quote timestamp, unrounded,
                         because a "successful" response on a sleeping name
                         can still be minutes or hours stale — that is
                         exactly what probes/quote_sanity.py's freshness gate
                         exists to catch, not this adapter's job to hide.
  - alpaca_bars_1m     — GET /v2/stocks/bars, batched, historical 1-minute
                         bars from today's 04:00 ET session start, paginated
                         via next_page_token. Same forming-bar-safe shape as
                         yfinance_bars_1m (last completed bar's volume kept
                         separate from the cumulative sum, which excludes the
                         still-forming bar) — but on real IEX data, which,
                         unlike yfinance's chart API, does NOT structurally
                         report zero volume in extended hours. Also sums
                         Alpaca's own per-bar trade count (`n`) into
                         `cumulative_trade_count` — the direct answer to "how
                         many trades actually arrived" for thin pre-market
                         names like RGTI/POET/QUBT, which is what decides
                         whether IEX's ~2% of the tape is enough or whether a
                         paid SIP feed becomes necessary.

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
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
ALPACA_DATA_BASE = "https://data.alpaca.markets/v2/stocks"
ALPACA_MAX_BAR_PAGES = 20  # hard stop so a pagination bug can never loop forever
HTTP_TIMEOUT_SECS = 10
ET = ZoneInfo("America/New_York")


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

    # Uncascaded, per-session-state fields — captured every tick regardless of
    # marketState, so a frozen field shows up directly in the data rather than
    # being inferred from a cascade that already picked a "winner." This is
    # the whole point of tonight's after-hours run: prove whether
    # regular_market_price/volume keep changing after 16:00 or freeze while
    # postmarket_* starts moving.
    row["regular_market_price"] = info.get("regularMarketPrice")
    row["regular_market_volume"] = info.get("regularMarketVolume")
    row["regular_market_time_raw"] = info.get("regularMarketTime")
    row["regular_market_time"] = _epoch_to_iso(info.get("regularMarketTime"))

    row["premarket_price"] = info.get("preMarketPrice")
    row["premarket_volume"] = info.get("preMarketVolume")
    row["premarket_time_raw"] = info.get("preMarketTime")
    row["premarket_time"] = _epoch_to_iso(info.get("preMarketTime"))

    row["postmarket_price"] = info.get("postMarketPrice")
    row["postmarket_volume"] = info.get("postMarketVolume")
    row["postmarket_time_raw"] = info.get("postMarketTime")
    row["postmarket_time"] = _epoch_to_iso(info.get("postMarketTime"))

    # `price`/`volume`/`source_ts` below stay a best-effort cascade (prefer
    # pre/post market over regular) purely for convenience in generic
    # cross-source queries. They are NOT the fields to use for freeze
    # detection — use the uncascaded columns above for that.
    if row["premarket_price"] is not None:
        row["price"] = row["premarket_price"]
        row["source_ts_raw"] = row["premarket_time_raw"]
        row["source_ts"] = row["premarket_time"]
    elif row["postmarket_price"] is not None:
        row["price"] = row["postmarket_price"]
        row["source_ts_raw"] = row["postmarket_time_raw"]
        row["source_ts"] = row["postmarket_time"]
    else:
        row["price"] = row["regular_market_price"]
        row["source_ts_raw"] = row["regular_market_time_raw"]
        row["source_ts"] = row["regular_market_time"]

    for vol_field, val in (
        ("preMarketVolume", row["premarket_volume"]),
        ("postMarketVolume", row["postmarket_volume"]),
        ("regularMarketVolume", row["regular_market_volume"]),
        ("volume", info.get("volume")),
    ):
        if val is not None:
            row["volume"] = val
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
    """period='1d', interval='1m', prepost=True returns bars starting at
    04:00 ET (empirically — see RUNBOOK.md), through a still-accumulating
    bar for the current minute. That last bar's Volume is a partial count
    for however many seconds have elapsed since the minute turned over — at
    :00 seconds past the minute it reads 0 regardless of how active the
    ticker actually is. This function never reports that number as "the"
    volume. It reports the last CLOSED bar's volume, and separately a
    cumulative sum of every closed bar since the window started, so the
    forming bar's real close price is still available without its
    misleading volume."""
    row = _base_row(probe, "yfinance_bars_1m", ticker)
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
    except Exception as e:  # noqa: BLE001
        row["error"] = f"{type(e).__name__}: {e}"
        return row

    if hist is None or hist.empty:
        row["status"] = "no_data"
        return row

    tz = hist.index.tz
    now = pd.Timestamp.now(tz=tz) if tz is not None else pd.Timestamp.now()
    bar_len = pd.Timedelta(minutes=1)

    # A bar has closed once its bucket end (start + 1m) is at or before now.
    is_forming = (hist.index[-1] + bar_len) > now
    forming = hist.iloc[-1] if is_forming else None
    forming_ts = hist.index[-1] if is_forming else None
    completed = hist.iloc[:-1] if is_forming else hist

    payload = {
        "bar_count": len(hist),
        "forming_bar_excluded": bool(is_forming),
        "window_start": str(hist.index[0]),
        "window_end": str(hist.index[-1]),
    }
    if forming is not None:
        forming_close = float(forming["Close"]) if forming["Close"] == forming["Close"] else None
        forming_volume = float(forming["Volume"]) if forming["Volume"] == forming["Volume"] else None
        payload["forming_bar"] = {"timestamp": str(forming_ts), "close": forming_close, "volume": forming_volume}
        row["forming_bar_ts"] = str(forming_ts)
        row["forming_bar_volume"] = forming_volume

    if completed.empty:
        # Only the forming bar exists so far (e.g. the very first minute
        # after 04:00 pre-market open) — no completed bar yet. That is a
        # real result, not an error.
        row["raw_payload"] = _safe_json(payload)
        row["price"] = payload.get("forming_bar", {}).get("close")
        row["status"] = "ok" if row["price"] is not None else "no_data"
        return row

    last_completed = completed.iloc[-1]
    last_completed_ts = completed.index[-1]
    last_completed_close = float(last_completed["Close"]) if last_completed["Close"] == last_completed["Close"] else None
    last_completed_volume = float(last_completed["Volume"]) if last_completed["Volume"] == last_completed["Volume"] else None

    cumulative_volume = float(completed["Volume"].sum()) if completed["Volume"].notna().any() else None
    cumulative_bar_count = int(completed["Volume"].notna().sum())

    payload["last_completed_bar"] = {
        "timestamp": str(last_completed_ts), "close": last_completed_close, "volume": last_completed_volume,
    }
    payload["cumulative_volume_since_open"] = cumulative_volume
    payload["cumulative_bar_count"] = cumulative_bar_count
    payload["cumulative_window_start_ts"] = str(hist.index[0])
    row["raw_payload"] = _safe_json(payload)

    row["source_ts_raw"] = str(last_completed_ts)
    try:
        row["source_ts"] = last_completed_ts.tz_convert("UTC").isoformat()
    except Exception:  # noqa: BLE001
        row["source_ts"] = None

    # Freshest available price still comes from the latest bar, forming or
    # not — a partial minute's Close is a real trade price, unlike its
    # Volume, which is misleadingly partial.
    row["price"] = payload["forming_bar"]["close"] if forming is not None else last_completed_close

    row["last_completed_bar_volume"] = last_completed_volume
    row["last_completed_bar_ts"] = str(last_completed_ts)
    row["cumulative_volume_since_open"] = cumulative_volume
    row["cumulative_bar_count"] = cumulative_bar_count
    row["cumulative_window_start_ts"] = payload["cumulative_window_start_ts"]

    # Canonical volume/volume_field: the last COMPLETED bar's volume, never the forming bar's.
    if last_completed_volume is not None:
        row["volume"] = last_completed_volume
        row["volume_field"] = "last_completed_bar_volume_1m"

    row["status"] = "ok" if row["price"] is not None else "no_data"
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


# ---------------------------------------------------------------------------
# Alpaca IEX — batched across all tickers, not one call per ticker (see
# module docstring for why)
# ---------------------------------------------------------------------------

def _iso_z_to_iso(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).isoformat()
    except (TypeError, ValueError):
        return None


def _alpaca_headers(key_id: str, secret_key: str) -> dict:
    return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}


def _et_session_start_utc_iso() -> str:
    """04:00 ET today, as a UTC ISO8601 string with a Z suffix — the same
    pre-market window start used throughout this codebase (see
    seismo/collect_quotes.py's _premarket_high_low)."""
    now_et = datetime.now(ET)
    start_et = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    return start_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _alpaca_missing_key_rows(probe: str, source: str, tickers: list[str]) -> dict[str, dict]:
    rows = {t: _base_row(probe, source, t) for t in tickers}
    for row in rows.values():
        row["error"] = "missing ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY"
    return rows


def fetch_alpaca_quotes_batch(probe: str, tickers: list[str], key_id: str | None,
                               secret_key: str | None) -> dict[str, dict]:
    """One HTTP call for the latest bid/ask across every ticker at once.
    Returns {ticker: row} for every requested ticker, including any Alpaca's
    response simply omits — recorded as no_data, never silently dropped from
    the returned dict, so a caller iterating `tickers` never KeyErrors."""
    rows = {t: _base_row(probe, "alpaca_quote", t) for t in tickers}
    if not key_id or not secret_key:
        return _alpaca_missing_key_rows(probe, "alpaca_quote", tickers)

    try:
        resp = requests.get(
            f"{ALPACA_DATA_BASE}/quotes/latest",
            params={"symbols": ",".join(tickers), "feed": "iex"},
            headers=_alpaca_headers(key_id, secret_key),
            timeout=HTTP_TIMEOUT_SECS,
        )
    except requests.RequestException as e:
        for row in rows.values():
            row["error"] = f"{type(e).__name__}: {e}"
        return rows

    for row in rows.values():
        row["http_status"] = resp.status_code

    if resp.status_code != 200:
        for row in rows.values():
            row["error"] = f"HTTP {resp.status_code}"
            row["raw_payload"] = _safe_json({"text": resp.text[:1000]})
        return rows

    try:
        data = resp.json()
    except ValueError as e:
        for row in rows.values():
            row["error"] = f"invalid JSON: {e}"
        return rows

    quotes = data.get("quotes") or {}
    for t in tickers:
        row = rows[t]
        q = quotes.get(t)
        if not q:
            row["status"] = "no_data"
            continue
        row["raw_payload"] = _safe_json(q)
        row["bid"] = q.get("bp")
        row["ask"] = q.get("ap")
        row["bid_size"] = q.get("bs")
        row["ask_size"] = q.get("as")
        row["source_ts_raw"] = q.get("t")
        row["source_ts"] = _iso_z_to_iso(q.get("t"))
        row["status"] = "ok" if (row["bid"] is not None or row["ask"] is not None) else "no_data"
    return rows


def fetch_alpaca_bars_1m_batch(probe: str, tickers: list[str], key_id: str | None,
                                secret_key: str | None) -> dict[str, dict]:
    """Historical 1-minute bars since today's 04:00 ET session start, one
    batched call (paginated via next_page_token — at ~90 tickers over a
    multi-hour window this regularly exceeds a single page). Same
    forming-bar-safe shape as fetch_yfinance_bars_1m: the still-accumulating
    current-minute bar's volume is kept separate from the cumulative sum,
    which only ever sums CLOSED bars. Additionally sums Alpaca's own per-bar
    trade count (`n`) into cumulative_trade_count — unlike yfinance's chart
    API, which structurally reports zero volume for every extended-hours
    bar, this is real IEX print activity."""
    rows = {t: _base_row(probe, "alpaca_bars_1m", t) for t in tickers}
    if not key_id or not secret_key:
        return _alpaca_missing_key_rows(probe, "alpaca_bars_1m", tickers)

    start = _et_session_start_utc_iso()
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bars_by_ticker: dict[str, list[dict]] = {t: [] for t in tickers}
    page_token = None
    pages = 0

    try:
        while True:
            params = {
                "symbols": ",".join(tickers), "timeframe": "1Min",
                "start": start, "end": end, "feed": "iex", "limit": 10000,
            }
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(
                f"{ALPACA_DATA_BASE}/bars", params=params,
                headers=_alpaca_headers(key_id, secret_key), timeout=HTTP_TIMEOUT_SECS,
            )
            if resp.status_code != 200:
                for row in rows.values():
                    row["error"] = f"HTTP {resp.status_code}"
                    row["http_status"] = resp.status_code
                return rows
            data = resp.json()
            for t, bars in (data.get("bars") or {}).items():
                bars_by_ticker.setdefault(t, []).extend(bars)
            pages += 1
            page_token = data.get("next_page_token")
            if not page_token or pages >= ALPACA_MAX_BAR_PAGES:
                break
    except requests.RequestException as e:
        for row in rows.values():
            row["error"] = f"{type(e).__name__}: {e}"
        return rows

    now_utc = datetime.now(timezone.utc)
    for t in tickers:
        row = rows[t]
        bars = bars_by_ticker.get(t) or []
        if not bars:
            row["status"] = "no_data"
            continue

        # Alpaca returns bars in chronological order. A bar has closed once
        # its bucket end (start + 1m) is at or before now — same rule as
        # fetch_yfinance_bars_1m.
        last_bar = bars[-1]
        last_bar_start = datetime.fromisoformat(last_bar["t"].replace("Z", "+00:00"))
        is_forming = (last_bar_start + timedelta(minutes=1)) > now_utc
        forming = last_bar if is_forming else None
        completed = bars[:-1] if is_forming else bars

        payload = {
            "bar_count": len(bars),
            "forming_bar_excluded": is_forming,
            "window_start": bars[0]["t"],
            "window_end": last_bar["t"],
        }
        if forming is not None:
            row["forming_bar_ts"] = forming["t"]
            row["forming_bar_volume"] = forming.get("v")
            payload["forming_bar"] = {"timestamp": forming["t"], "close": forming.get("c"), "volume": forming.get("v")}

        if not completed:
            row["raw_payload"] = _safe_json(payload)
            row["price"] = forming.get("c") if forming else None
            row["status"] = "ok" if row["price"] is not None else "no_data"
            continue

        last_completed = completed[-1]
        cumulative_volume = sum(b.get("v", 0) or 0 for b in completed)
        cumulative_trade_count = sum(b.get("n", 0) or 0 for b in completed)
        session_high = max((b["h"] for b in completed if b.get("h") is not None), default=None)
        session_low = min((b["l"] for b in completed if b.get("l") is not None), default=None)

        payload["last_completed_bar"] = {
            "timestamp": last_completed["t"], "close": last_completed.get("c"), "volume": last_completed.get("v"),
        }
        payload["cumulative_volume_since_open"] = cumulative_volume
        payload["cumulative_bar_count"] = len(completed)
        payload["cumulative_trade_count"] = cumulative_trade_count
        payload["session_high"] = session_high
        payload["session_low"] = session_low
        row["raw_payload"] = _safe_json(payload)

        row["source_ts_raw"] = last_completed["t"]
        row["source_ts"] = _iso_z_to_iso(last_completed["t"])
        row["price"] = forming.get("c") if forming is not None else last_completed.get("c")
        row["last_completed_bar_volume"] = last_completed.get("v")
        row["last_completed_bar_ts"] = last_completed["t"]
        row["cumulative_volume_since_open"] = cumulative_volume
        row["cumulative_bar_count"] = len(completed)
        row["cumulative_trade_count"] = cumulative_trade_count
        # Not part of probes/db.py's OBSERVATION_COLUMNS (session high/low
        # isn't a probe-schema field) — these two are read directly by
        # seismo/collect_quotes.py, which uses the dict, not the DB row.
        row["session_high"] = session_high
        row["session_low"] = session_low

        if row["last_completed_bar_volume"] is not None:
            row["volume"] = row["last_completed_bar_volume"]
            row["volume_field"] = "last_completed_bar_volume_1m"

        row["status"] = "ok" if row["price"] is not None else "no_data"

    return rows
