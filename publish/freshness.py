"""publish/freshness.py — publish source-lag and field-freeze summaries from probe.sqlite.

Reads /srv/premonition/db/probe.sqlite (raw, per-tick observations collected by
probes/freshness.py and probes/friction.py) and writes two SUMMARIES to Supabase:
probe_source_freshness (how stale is each source, typically) and
probe_field_behavior (does each Yahoo field keep changing after the 16:00 close,
or correctly freeze — see probes/sources.py's fetch_yfinance_quote docstring for
why this matters).

This is the one place in the project that computes a statistic ahead of a human
looking at it — and it's fine here, unlike in a brief, because:
  1. This is Probe A/B's own stated purpose (DESIGN.md phase 1: "I will analyze
     the data" — a summary IS the analysis, not a trading claim).
  2. Every number here carries its own provenance: sample_count, window_start/end,
     and (for field behavior) how many non-null ticks it's based on. A reader can
     always tell whether a status is well-supported or is 'insufficient_data'.
  3. Nothing here ever renders as a recommendation to trade anything.

Run manually or on a schedule from the agents laptop, never from Vercel:
    python3 -m publish.freshness --dry-run
    python3 -m publish.freshness
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from probes.db import DEFAULT_DB_PATH
from publish import supabase_rest

ET = ZoneInfo("America/New_York")
MIN_AFTER_CLOSE_TICKS = 3       # fewer than this and we say so, rather than guess
FROZEN_TRAILING_TICKS = 10      # ~5 min at the standard 30s poll interval

# Classification formula, versioned like probes/friction.py's spread_proxy_formula:
# a whole-window "did the value ever change" count is misleading on its own — Yahoo's
# regularMarketPrice/Volume typically take a few closing-print corrections in the first
# ~60-90s after 16:00 (the closing auction settling) and then hold flat for the rest of
# the session, while postMarketPrice changes on nearly every tick throughout. What
# matters for "would this look live at 08:30 tomorrow" is whether the value is CURRENTLY
# flat, not whether it was ever flat. So: look at the trailing run of same-valued ticks.
# If the most recent FROZEN_TRAILING_TICKS are all equal, call it frozen — regardless of
# how much it moved earlier in the window.
FIELD_BEHAVIOR_FORMULA = "trailing_run_v1"

# (db column, Yahoo's own field name) — the nine uncascaded fields written by
# probes/sources.py's fetch_yfinance_quote.
QUOTE_FIELDS = [
    ("regular_market_price", "regularMarketPrice"),
    ("regular_market_volume", "regularMarketVolume"),
    ("regular_market_time", "regularMarketTime"),
    ("premarket_price", "preMarketPrice"),
    ("premarket_volume", "preMarketVolume"),
    ("premarket_time", "preMarketTime"),
    ("postmarket_price", "postMarketPrice"),
    ("postmarket_volume", "postMarketVolume"),
    ("postmarket_time", "postMarketTime"),
]


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _et_date_bounds(date_et) -> tuple[datetime, datetime, datetime]:
    """Return (day_start, close_1600, day_end) in ET for the given date."""
    day_start = datetime(date_et.year, date_et.month, date_et.day, 0, 0, tzinfo=ET)
    close = datetime(date_et.year, date_et.month, date_et.day, 16, 0, tzinfo=ET)
    day_end = day_start + timedelta(days=1)
    return day_start, close, day_end


def compute_source_freshness(conn: sqlite3.Connection) -> list[dict]:
    rows = []
    cur = conn.execute(
        "SELECT probe, source, status, fetched_at, source_ts, error FROM observations"
    )
    by_key: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in cur:
        by_key.setdefault((r["probe"], r["source"]), []).append(r)

    for (probe, source), obs in by_key.items():
        attempt_count = len(obs)
        errors = [o for o in obs if o["status"] != "ok"]
        oks_with_ts = [o for o in obs if o["status"] == "ok" and o["source_ts"]]

        lags = []
        for o in oks_with_ts:
            try:
                fetched = _parse_iso(o["fetched_at"])
                source_ts = _parse_iso(o["source_ts"])
                lags.append((fetched - source_ts).total_seconds())
            except (TypeError, ValueError):
                continue  # unparseable timestamp — excluded, not treated as 0 lag

        fetched_ats = [_parse_iso(o["fetched_at"]) for o in obs]
        last_error = None
        if errors:
            most_recent_error = max(errors, key=lambda o: o["fetched_at"])
            last_error = most_recent_error["error"]

        rows.append({
            "probe": probe,
            "source": source,
            "sample_count": len(lags),
            "median_lag_secs": statistics.median(lags) if lags else None,
            "min_lag_secs": min(lags) if lags else None,
            "max_lag_secs": max(lags) if lags else None,
            "attempt_count": attempt_count,
            "error_count": len(errors),
            "error_rate": (len(errors) / attempt_count) if attempt_count else None,
            "last_error": last_error,
            "window_start": min(fetched_ats).isoformat() if fetched_ats else None,
            "window_end": max(fetched_ats).isoformat() if fetched_ats else None,
        })
    return rows


def _classify_one_ticker(values_in_order: list) -> tuple[str, int, int]:
    """values_in_order: one ticker's non-null values for one field, after close,
    in chronological order. Returns (status, total_changes, trailing_run)."""
    if len(values_in_order) < MIN_AFTER_CLOSE_TICKS:
        return "insufficient_data", 0, 0

    last_value = values_in_order[-1]
    trailing_run = 0
    for v in reversed(values_in_order):
        if v == last_value:
            trailing_run += 1
        else:
            break

    distinct_values = len(set(values_in_order))
    total_changes = distinct_values - 1
    is_frozen_now = trailing_run >= min(FROZEN_TRAILING_TICKS, len(values_in_order))
    return ("freezes_after_close" if is_frozen_now else "updates_after_close"), total_changes, trailing_run


def compute_field_behavior(conn: sqlite3.Connection, date_et) -> list[dict]:
    day_start, close, day_end = _et_date_bounds(date_et)
    cur = conn.execute(
        "SELECT ticker, fetched_at, raw_payload, " + ", ".join(c for c, _ in QUOTE_FIELDS) +
        " FROM observations WHERE probe = 'freshness' AND source = 'yfinance_quote' "
        "AND status = 'ok' ORDER BY fetched_at"
    )
    all_rows = cur.fetchall()

    before, after = [], []
    for r in all_rows:
        ts = _parse_iso(r["fetched_at"]).astimezone(ET)
        if not (day_start <= ts < day_end):
            continue
        (before if ts < close else after).append(r)

    tickers_covered = sorted({r["ticker"] for r in before + after})
    window_start = min((_parse_iso(r["fetched_at"]) for r in before + after), default=None)
    window_end = max((_parse_iso(r["fetched_at"]) for r in before + after), default=None)

    # Group AFTER-close rows per ticker, in chronological order — mixing tickers
    # together would make every field look like it's "constantly changing" purely
    # because NVDA's price differs from AAPL's, regardless of either one's real
    # freeze behavior. Classification runs per ticker; the field's overall status
    # is the honest rollup of however many tickers actually froze vs. didn't.
    after_by_ticker: dict[str, list] = {}
    for r in after:
        after_by_ticker.setdefault(r["ticker"], []).append(r)

    results = []
    for col, yahoo_name in QUOTE_FIELDS:
        before_vals = {r[col] for r in before if r[col] is not None}

        per_ticker = {}
        for ticker, rows in after_by_ticker.items():
            values = [r[col] for r in rows if r[col] is not None]
            status, changes, trailing = _classify_one_ticker(values)
            per_ticker[ticker] = {"status": status, "changes": changes,
                                   "trailing": trailing, "ticks": len(values)}

        decided = {t: v for t, v in per_ticker.items() if v["status"] != "insufficient_data"}
        total_ticks_after = sum(v["ticks"] for v in per_ticker.values())

        if not decided:
            status = "insufficient_data"
            if col.startswith("premarket_"):
                # This after-close window will never contain a PRE market state — its
                # absence here says nothing about whether Yahoo provides the field.
                # That question belongs to tomorrow's actual pre-market probe run.
                note = ("market has not been in a PRE state during this after-close window "
                        "(expected) — check again during a pre-market run, e.g. tomorrow 08:00-09:30 ET")
            else:
                # regular_market_*/postmarket_* fields DO belong to states we're confirmed
                # to have been in during this window (regular session, then POST). A
                # zero-tick field here is ambiguous on its own: either "not enough ticks
                # yet" or "this source's free tier never sends this key at all" (true for
                # postMarketVolume — confirmed by checking the raw payload directly rather
                # than assuming). Sample a few raw payloads — if the key never appears
                # despite the state being right, say so plainly.
                sampled = after[-5:]
                key_ever_present = any(yahoo_name in json.loads(r["raw_payload"]) for r in sampled)
                if sampled and not key_ever_present:
                    note = (f"the key '{yahoo_name}' does not appear at all in the last "
                            f"{len(sampled)} raw payloads sampled from after 16:00 ET, despite the "
                            f"market having been in the relevant state — this source's free tier "
                            f"appears not to expose this field, not merely 'not yet available'")
                else:
                    note = (f"fewer than {MIN_AFTER_CLOSE_TICKS} non-null ticks after 16:00 ET for "
                            f"any covered ticker so far — after-hours run may still be in progress")
        else:
            frozen = [t for t, v in decided.items() if v["status"] == "freezes_after_close"]
            updating = [t for t, v in decided.items() if v["status"] == "updates_after_close"]
            if updating:
                status = "updates_after_close"
                note = (f"still changing for {len(updating)}/{len(decided)} ticker(s) "
                        f"({', '.join(sorted(updating))}) as of the latest tick")
            else:
                status = "freezes_after_close"
                changed_first = [t for t in frozen if per_ticker[t]["changes"] > 0]
                if changed_first:
                    note = (f"frozen for all {len(frozen)} covered ticker(s) — each took a few "
                            f"closing-print correction(s) in the minute or so after 16:00, then "
                            f"held constant since")
                else:
                    note = f"constant for all {len(frozen)} covered ticker(s) across the entire after-close window"

        results.append({
            "field": col,
            "yahoo_field_name": yahoo_name,
            "status": status,
            "evidence_note": note,
            "classification_formula": FIELD_BEHAVIOR_FORMULA,
            "distinct_values_before_close": len(before_vals),
            "distinct_values_after_close": len({v for rows in after_by_ticker.values()
                                                 for r in rows if (v := r[col]) is not None}),
            "ticks_after_close": total_ticks_after,
            "tickers_covered": tickers_covered,
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window_end else None,
        })
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish freshness/field-behavior summaries to Supabase")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--date", default=None,
                         help="ET calendar date (YYYY-MM-DD) for field-behavior close-freeze analysis; defaults to today")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    date_et = (datetime.strptime(args.date, "%Y-%m-%d").date()
               if args.date else datetime.now(ET).date())

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    source_rows = compute_source_freshness(conn)
    field_rows = compute_field_behavior(conn, date_et)
    conn.close()

    print(f"{len(source_rows)} source-freshness rows, {len(field_rows)} field-behavior rows")
    for r in field_rows:
        print(f"  {r['yahoo_field_name']:22s} {r['status']:22s} {r['evidence_note']}")

    if args.dry_run:
        return 0

    supabase_rest.upsert("probe_source_freshness", source_rows, on_conflict="probe,source")
    supabase_rest.upsert("probe_field_behavior", field_rows, on_conflict="field")
    print("published to Supabase")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
