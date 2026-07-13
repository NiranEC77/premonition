"""epicenter/grade.py — the 16:15 grade. Actual outcomes vs. our picks vs. two dumb baselines.

DESIGN.md 5: "Magnitude: |open -> 09:45| for our picks, vs. the universe
average, vs. the naive baseline — 'just pick the biggest pre-market gaps.'
That baseline is the bar." This grades against 09:30-09:35 (5 minutes after
the open) rather than 09:45 — a tighter, earlier window that's less
contaminated by news drift later in the opening range; the target concept is
identical, magnitude of the immediate opening move.

Requires today's actual 1-minute bars, which yfinance only carries for the
trailing 8 days — this must run the SAME day as the session it's grading,
same-day, at or after 16:15 ET, or the data is gone.

Usage:
    python3 -m epicenter.grade [--dry-run] [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import statistics
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

from epicenter import tradability
from publish import supabase_rest
from seismo import facts_db
from seismo.universe import resolved_tickers

ET = ZoneInfo("America/New_York")


def _opening_range(ticker: str, session_date: str) -> dict | None:
    """Fetch today's actual open, and high/low/price in the 09:30-09:35 ET
    window, from real 1-minute bars. None if the session isn't in the
    trailing-8-day window yfinance carries, or the ticker has no bars."""
    try:
        hist = yf.Ticker(ticker).history(period="8d", interval="1m", prepost=False)
    except Exception:  # noqa: BLE001
        return None
    if hist is None or hist.empty:
        return None

    day = hist[hist.index.strftime("%Y-%m-%d") == session_date]
    if day.empty:
        return None

    open_price = float(day.iloc[0]["Open"])
    window = day.between_time("09:30", "09:35")
    if window.empty:
        return None

    return {
        "open_price": open_price,
        "high_0935": float(window["High"].max()),
        "low_0935": float(window["Low"].min()),
        "price_0935": float(window.iloc[-1]["Close"]),
    }


def grade_universe(session_date: str, tickers: list[str]) -> dict[str, dict]:
    """Actual opening-range outcome for every resolved ticker — not just our
    picks. Needed both to grade our picks AND to compute what the two dumb
    baselines would have shown."""
    results = {}
    for ticker in tickers:
        r = _opening_range(ticker, session_date)
        if r is None:
            continue
        rng_pct = (r["high_0935"] - r["low_0935"]) / r["open_price"] * 100 if r["open_price"] else None
        results[ticker] = {**r, "actual_range_pct": rng_pct}
    return results


def compute_baselines(conn, session_date: str, universe_outcomes: dict[str, dict]) -> dict:
    """The two dumb strategies the model has to beat, per CLAUDE.md/DESIGN.md:
    'just pick the 6 biggest overnight movers' and 'just pick the 6 with the
    most overnight trading.' Built from THIS MORNING's quotes snapshot
    (premarket_gap_pct / premarket_volume, captured at lock time) —  not
    reconstructed after the fact from what worked out well."""
    quotes = conn.execute(
        "SELECT ticker, premarket_gap_pct, premarket_volume FROM quotes"
    ).fetchall()

    by_gap = sorted((q for q in quotes if q[1] is not None), key=lambda q: abs(q[1]), reverse=True)
    by_vol = sorted((q for q in quotes if q[2] is not None), key=lambda q: q[2], reverse=True)

    gap_top6 = [q[0] for q in by_gap[:6]]
    vol_top6 = [q[0] for q in by_vol[:6]]

    def mean_range(tickers: list[str]) -> float | None:
        vals = [universe_outcomes[t]["actual_range_pct"] for t in tickers
                if t in universe_outcomes and universe_outcomes[t]["actual_range_pct"] is not None]
        return statistics.mean(vals) if vals else None

    all_ranges = [v["actual_range_pct"] for v in universe_outcomes.values() if v["actual_range_pct"] is not None]

    return {
        "session_date": session_date,
        "naive_gap_top6": gap_top6,
        "naive_mean_range": mean_range(gap_top6),
        "naive_rvol_top6": vol_top6,
        "rvol_mean_range": mean_range(vol_top6),
        "universe_mean_range": statistics.mean(all_ranges) if all_ranges else None,
        "our_mean_range": None,  # filled in by grade_picks once picks are known
    }


def grade_picks(conn, session_date: str, universe_outcomes: dict[str, dict], thresholds: dict) -> list[dict]:
    # PostgREST has no subselect over REST — fetch the brief id first, then its picks.
    brief_resp = supabase_rest.select("briefs", {"select": "id", "session_date": f"eq.{session_date}", "stage": "eq.lock"})
    briefs = brief_resp.json() if brief_resp.status_code == 200 else []
    if not briefs:
        return []
    brief_id = briefs[0]["id"]

    picks_resp = supabase_rest.select("picks", {"select": "*", "brief_id": f"eq.{brief_id}"})
    picks = picks_resp.json() if picks_resp.status_code == 200 else []

    grades = []
    ranked = sorted(universe_outcomes.items(), key=lambda kv: kv[1]["actual_range_pct"] or -1, reverse=True)
    rank_of = {ticker: i + 1 for i, (ticker, _) in enumerate(ranked)}

    for pick in picks:
        ticker = pick["ticker"]
        outcome = universe_outcomes.get(ticker)
        if outcome is None:
            continue

        gap_pct = pick.get("premarket_gap_pct") or 0
        continued = None
        if outcome["price_0935"] is not None and outcome["open_price"]:
            moved_pct = (outcome["price_0935"] - outcome["open_price"]) / outcome["open_price"] * 100
            continued = (moved_pct > 0) == (gap_pct > 0)

        brier = None
        if pick.get("p_continuation") is not None and continued is not None:
            brier = (pick["p_continuation"] - (1.0 if continued else 0.0)) ** 2

        was_tradable, _ = tradability.check(
            {"price": outcome["open_price"], "premarket_volume": pick.get("premarket_volume"),
             "spread_pct": None, "float_shares": None},
            thresholds,
        )

        grades.append({
            "pick_id": pick["id"],
            "session_date": session_date,
            "open_price": outcome["open_price"],
            "high_0935": outcome["high_0935"],
            "low_0935": outcome["low_0935"],
            "price_0935": outcome["price_0935"],
            "actual_range_pct": outcome["actual_range_pct"],
            "continued": continued,
            "brier": brier,
            "actual_rank": rank_of.get(ticker),
            "was_tradable": was_tradable,
        })

    return grades


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="Session date YYYY-MM-DD, ET. Defaults to today.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    session_date = args.date or datetime.now(ET).date().isoformat()
    tickers = [t["ticker"] for t in resolved_tickers()]

    print(f"grading session {session_date} across {len(tickers)} tickers")
    universe_outcomes = grade_universe(session_date, tickers)
    print(f"got real 09:30-09:35 outcomes for {len(universe_outcomes)}/{len(tickers)} tickers")

    conn = facts_db.connect()
    thresholds = tradability.load_thresholds()
    baselines = compute_baselines(conn, session_date, universe_outcomes)
    grades = grade_picks(conn, session_date, universe_outcomes, thresholds)
    conn.close()

    if grades:
        baselines["our_mean_range"] = statistics.mean(
            g["actual_range_pct"] for g in grades if g["actual_range_pct"] is not None
        )

    print(f"\nbaselines: {baselines}")
    print(f"\n{len(grades)} pick(s) graded:")
    for g in grades:
        print(f"  pick_id={g['pick_id']} range={g['actual_range_pct']} "
              f"continued={g['continued']} brier={g['brier']} rank={g['actual_rank']}")

    if args.dry_run:
        print("\n--dry-run: not writing to Supabase")
        return 0

    supabase_rest.upsert("baselines", [baselines], on_conflict="session_date")
    if grades:
        supabase_rest.upsert("grades", grades, on_conflict="pick_id")
    print("published to Supabase")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
