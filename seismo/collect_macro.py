"""seismo/collect_macro.py — overnight world signals: crypto + index futures.

DESIGN.md: "BTC/ETH overnight — free crypto API. Trades 24/7. Predicts 9
names. Highest value-per-line-of-code in the repo." Also the futures/index
context that leads the semis complex (TSM in Taipei, Nikkei, DAX) and the
dollar (DXY).

As of 2026-07-16, CoinGecko is the PRIMARY source for BTC-USD/ETH-USD
specifically — a dedicated, always-on crypto API rather than yfinance's
generic quote endpoint, which is the source everywhere else in this project
found to freeze or misbehave around session boundaries. yfinance remains the
fallback for BTC/ETH if CoinGecko fails, and stays the ONLY source for the
index futures/DXY symbols, which CoinGecko has no coverage for at all.

Usage:
    python3 -m seismo.collect_macro [--dry-run]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import requests
import yfinance as yf

from seismo import facts_db

SYMBOLS = [
    ("BTC-USD", "Bitcoin"),
    ("ETH-USD", "Ethereum"),
    ("ES=F", "S&P 500 futures"),
    ("NQ=F", "Nasdaq 100 futures"),
    ("CL=F", "WTI Crude Oil futures"),
    ("^N225", "Nikkei 225"),
    ("^TWII", "Taiwan Weighted (TSM leads semis)"),
    ("^GDAXI", "DAX"),
    ("DX-Y.NYB", "US Dollar Index"),
]

# Maps our symbol naming onto CoinGecko's own id scheme — only BTC/ETH have
# a CoinGecko path; everything else falls straight through to yfinance.
COINGECKO_IDS = {"BTC-USD": "bitcoin", "ETH-USD": "ethereum"}
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
HTTP_TIMEOUT_SECS = 10


def _coingecko_batch(symbols: list[str]) -> tuple[dict[str, dict], str | None]:
    """One call for every CoinGecko-covered symbol at once. Returns
    ({symbol: {price, prev_close, change_pct}}, error) — error is set only
    on a request-level failure; a symbol CoinGecko simply has nothing for is
    just absent from the returned dict, not an error."""
    ids = [COINGECKO_IDS[s] for s in symbols if s in COINGECKO_IDS]
    if not ids:
        return {}, None
    try:
        resp = requests.get(
            COINGECKO_URL,
            params={"ids": ",".join(ids), "vs_currencies": "usd",
                     "include_24hr_change": "true", "include_last_updated_at": "true"},
            timeout=HTTP_TIMEOUT_SECS,
        )
    except requests.RequestException as e:
        return {}, f"{type(e).__name__}: {e}"

    if resp.status_code != 200:
        return {}, f"HTTP {resp.status_code}"

    try:
        data = resp.json()
    except ValueError as e:
        return {}, f"invalid JSON: {e}"

    id_to_symbol = {v: k for k, v in COINGECKO_IDS.items()}
    out = {}
    for cg_id, payload in data.items():
        symbol = id_to_symbol.get(cg_id)
        price = payload.get("usd")
        change_pct = payload.get("usd_24h_change")
        if symbol is None or price is None:
            continue
        # 24h rolling change is the natural equivalent of "change since prior
        # close" for an asset with no market close at all — derived
        # prev_close is an approximation, kept for schema consistency with
        # every other row in this table, not presented as CoinGecko's own number.
        prev_close = price / (1 + change_pct / 100) if change_pct is not None else None
        out[symbol] = {"price": price, "prev_close": prev_close, "change_pct": change_pct}
    return out, None


def collect(conn, symbols: list[tuple[str, str]]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    ok, failed = 0, 0

    coingecko_rows, coingecko_error = _coingecko_batch([s for s, _ in symbols])
    if coingecko_error:
        print(f"  coingecko: ERROR {coingecko_error} — BTC/ETH will fall back to yfinance")

    for symbol, label in symbols:
        cg = coingecko_rows.get(symbol)
        if cg is not None:
            conn.execute(
                "INSERT OR REPLACE INTO macro_quotes (symbol, label, price, prev_close, change_pct, source, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (symbol, label, cg["price"], cg["prev_close"], cg["change_pct"], "coingecko", now),
            )
            print(f"  {symbol} ({label}): {cg['price']} ({cg['change_pct']:+.2f}%) [coingecko]"
                  if cg["change_pct"] is not None else f"  {symbol} ({label}): {cg['price']} [coingecko]")
            ok += 1
            continue

        try:
            info = yf.Ticker(symbol).get_info()
        except Exception as e:  # noqa: BLE001
            print(f"  {symbol}: ERROR {type(e).__name__}: {e}")
            failed += 1
            continue

        price = info.get("regularMarketPrice") if info else None
        prev_close = info.get("regularMarketPreviousClose") if info else None
        change_pct = None
        if price is not None and prev_close:
            change_pct = (price - prev_close) / prev_close * 100

        if price is None:
            print(f"  {symbol}: no_data")
            failed += 1
            continue

        conn.execute(
            "INSERT OR REPLACE INTO macro_quotes (symbol, label, price, prev_close, change_pct, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol, label, price, prev_close, change_pct, "yfinance_info", now),
        )
        print(f"  {symbol} ({label}): {price} ({change_pct:+.2f}%) [yfinance]" if change_pct is not None
              else f"  {symbol} ({label}): {price} (no prev_close) [yfinance]")
        ok += 1

    conn.commit()
    return ok, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    symbols = SYMBOLS[:2] if args.dry_run else SYMBOLS
    print(f"collecting macro quotes for {len(symbols)} symbols")
    conn = facts_db.connect()
    ok, failed = collect(conn, symbols)
    conn.close()
    print(f"done: {ok} ok, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
