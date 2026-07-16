"""epicenter/publish_lock.py — the 09:15 lock. Writes shadow-mode picks to Supabase.

Runs the scorer, then publishes whatever it finds — zero picks is a valid,
honest outcome (CLAUDE.md: "Publish fewer than 6 if fewer than 6 clear the
bar. Never pad the list.") A brief row is written either way, so a morning
with zero qualifying names is a recorded fact, not silence.

shadow_mode is hardcoded true here — see supabase/migrations/0002. This
script does not decide when that changes; the backtest gate does.

Usage:
    python3 -m epicenter.publish_lock [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from epicenter import tradability
from epicenter.score import Candidate, load_weights, score_all, select_picks
from publish import supabase_rest
from seismo import facts_db

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO_ROOT / "universe.yaml"


def _halt_prone_set() -> set[str]:
    """Coarse, manually-seeded proxy — universe.yaml's watch_for_illiquidity
    list, NOT real LULD halt history (no collector for that exists yet).
    Labeled as such wherever it's shown."""
    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    return set(universe.get("watch_for_illiquidity", []))


def _market_context(conn) -> dict:
    """NQ/CL futures level plus HER OWN trading-rule flags — displayed back
    to her as her discipline, never framed as premonition's recommendation
    (see how-this-works.astro / the dashboard copy for that framing). The
    flag logic below is specified exactly as she gave it, verbatim — not
    derived, not tuned, not "improved."

      CL (oil): up >=5% -> red "strongly consider no trading"
                up >=3% -> yellow "caution — consider reducing position to half size"
                otherwise -> green "normal trading day"
      NQ (Nasdaq): up >=0.5% -> green "normal trading day, bias long"
                   down >=1% -> red "high alert — consider sitting out"
                   otherwise -> yellow "caution — consider reducing position to half size"

    FLAGGED, NOT CHANGED: NQ's "otherwise" band covers everything from -1%
    (exclusive) up to +0.5% (exclusive) — a dead-flat 0.0% morning and a
    -0.8% morning both show yellow. That may be exactly intended (yellow as
    "no strong signal either way"), but it's worth confirming with her
    before treating this as final. Built exactly as specified either way."""
    def _quote(symbol: str) -> dict | None:
        row = conn.execute(
            "SELECT price, prev_close, change_pct FROM macro_quotes WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        price, prev_close, change_pct = row
        return {
            "level": price,
            "change_abs": (price - prev_close) if prev_close is not None else None,
            "change_pct": change_pct,
        }

    cl = _quote("CL=F")
    if cl is not None:
        pct = cl["change_pct"] or 0
        if pct >= 5:
            cl.update(flag_color="red", flag_emoji="\U0001F534", flag_message="strongly consider no trading")
        elif pct >= 3:
            cl.update(flag_color="yellow", flag_emoji="\U0001F7E1",
                      flag_message="caution — consider reducing position to half size")
        else:
            cl.update(flag_color="green", flag_emoji="\U0001F7E2", flag_message="normal trading day")

    nq = _quote("NQ=F")
    if nq is not None:
        pct = nq["change_pct"] or 0
        if pct >= 0.5:
            nq.update(flag_color="green", flag_emoji="\U0001F7E2", flag_message="normal trading day, bias long")
        elif pct <= -1:
            nq.update(flag_color="red", flag_emoji="\U0001F534", flag_message="high alert — consider sitting out")
        else:
            nq.update(flag_color="yellow", flag_emoji="\U0001F7E1",
                      flag_message="caution — consider reducing position to half size")

    return {"nq": nq, "cl": cl}


def _macro_headlines(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT headline, source, source_url, published_at, category FROM macro_headlines "
        "ORDER BY published_at DESC"
    ).fetchall()
    return [
        {"headline": r[0], "source": r[1], "source_url": r[2], "published_at": r[3], "category": r[4]}
        for r in rows
    ]


def _recent_catalysts(conn, ticker: str, hours: int = 72, limit: int = 5) -> list[dict]:
    """The catalyst evidence behind a pick — every headline within the last
    72h, not just the single freshest one score.py uses for scoring. Capped
    at 5 so a heavily-covered name doesn't crowd the card; 72h and 5 are
    display choices, not scoring inputs — score.py's catalyst_freshness
    feature is untouched by this."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT headline, source, source_url, published_at, fetched_at FROM catalysts "
        "WHERE ticker = ? AND COALESCE(published_at, fetched_at) >= ? "
        "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?",
        (ticker, cutoff, limit),
    ).fetchall()
    return [
        {"text": r[0], "source": r[1], "source_url": r[2], "source_ts": r[3] or r[4]}
        for r in rows
    ]


def _cluster_story(conn) -> str | None:
    row = conn.execute(
        "SELECT change_pct FROM macro_quotes WHERE symbol = 'BTC-USD'"
    ).fetchone()
    if row and row[0] is not None and abs(row[0]) >= 3:
        direction = "fell" if row[0] < 0 else "rose"
        return f"Bitcoin {direction} {abs(row[0]):.1f}% overnight — crypto-complex names are likely moving together, not on individual news."
    return None


def _data_age_secs(conn) -> int | None:
    row = conn.execute("SELECT MAX(fetched_at) FROM quotes").fetchone()
    if not row or not row[0]:
        return None
    fetched = datetime.fromisoformat(row[0])
    return int((datetime.now(timezone.utc) - fetched).total_seconds())


def _build_reason(c: Candidate) -> dict:
    if c.catalyst_headline:
        return {"text": c.catalyst_headline, "source_url": c.catalyst_source_url,
                "source_ts": c.catalyst_published_at}
    gap = c.premarket_gap_pct or 0
    if c.rvol:
        text = f"Gapping {gap:+.1f}% pre-market on {c.rvol:.1f}x its typical pre-market volume."
    else:
        text = f"Gapping {gap:+.1f}% pre-market; not enough volume history yet to say if that's unusual for this name."
    return {"text": text, "source_url": None, "source_ts": None}


def build_brief_and_picks(conn, halt_prone_set: set[str]) -> tuple[dict, list[dict]]:
    weights = load_weights()
    thresholds = tradability.load_thresholds()
    candidates = score_all(conn, weights, thresholds)
    picks = select_picks(candidates, weights["max_picks"], weights["max_per_cluster"])

    session_date = datetime.now(ET).date().isoformat()
    brief = {
        "session_date": session_date,
        "stage": "lock",
        "status": "published",
        "cluster_story": _cluster_story(conn),
        "data_age_secs": _data_age_secs(conn),
        "verify_rejects": 0,  # no verifier this round — see task history, not silently claimed run
        "shadow_mode": True,
        "market_context": _market_context(conn),
        "macro_headlines": _macro_headlines(conn),
    }

    pick_rows = []
    for rank, c in enumerate(picks, 1):
        catalyst_reasons = _recent_catalysts(conn, c.ticker)
        pick_rows.append({
            "ticker": c.ticker,
            "company_name": c.company_name,
            "rank": rank,
            "score": round(c.score, 4),
            "score_breakdown": c.breakdown,
            "expected_move_pct": round(abs(c.premarket_gap_pct), 3) if c.premarket_gap_pct is not None else None,
            "premarket_gap_pct": round(c.premarket_gap_pct, 3) if c.premarket_gap_pct is not None else None,
            "premarket_rvol": round(c.rvol, 3) if c.rvol is not None else None,
            "spread_est": round(c.spread_est, 4) if c.spread_est is not None else None,
            "p_continuation": round(c.p_continuation, 3) if c.p_continuation is not None else None,
            "cluster": c.cluster,
            "halt_prone": c.ticker in halt_prone_set,
            "recent_ipo": c.recent_ipo,
            "levels": c.levels,
            "reasons": catalyst_reasons if catalyst_reasons else [_build_reason(c)],
            "demoted_note": None,
        })

    return brief, pick_rows, candidates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = facts_db.connect()
    halt_prone_set = _halt_prone_set()
    brief, pick_rows, candidates = build_brief_and_picks(conn, halt_prone_set)
    conn.close()

    print(f"brief: {json.dumps(brief, indent=2, default=str)}")
    print(f"\n{len(pick_rows)} pick(s):")
    for p in pick_rows:
        print(f"  {p['rank']}. {p['ticker']} score={p['score']} cluster={p['cluster']} "
              f"halt_prone={p['halt_prone']} recent_ipo={p['recent_ipo']}")
        print(f"     reason: {p['reasons'][0]['text']}")

    if args.dry_run:
        print("\n--dry-run: not writing to Supabase")
        return 0

    resp = supabase_rest.upsert("briefs", [brief], on_conflict="session_date,stage")
    if resp is None:
        print("no brief row to publish (unexpected — build_brief_and_picks always returns one)")
        return 1
    brief_id = resp.json()[0]["id"]
    print(f"published brief id={brief_id}")

    if pick_rows:
        for p in pick_rows:
            p["brief_id"] = brief_id
        supabase_rest.upsert("picks", pick_rows, on_conflict="brief_id,ticker")
        print(f"published {len(pick_rows)} pick(s)")
    else:
        print("0 picks cleared the bar tonight — publishing the brief alone, not padding the list")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
