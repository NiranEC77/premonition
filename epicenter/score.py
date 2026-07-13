"""epicenter/score.py — deterministic scorer. No LLM anywhere in this file.

DESIGN.md 3a: score is proportional to expected move / that ticker's OWN
typical move — surprise, not raw magnitude, so a quantum name gapping 8% on
nothing doesn't automatically outrank NVDA gapping 4% on real news.

Every number this produces is a fixed function of rows already in
facts.sqlite. There is no judgment call, no LLM, and nothing here changes
run to run except the input data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from epicenter import tradability
from seismo import facts_db
from seismo.universe import resolved_tickers

REPO_ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_PATH = REPO_ROOT / "weights.yaml"


def load_weights() -> dict:
    return yaml.safe_load(WEIGHTS_PATH.read_text())


@dataclass
class Candidate:
    ticker: str
    cluster: str
    recent_ipo: bool
    gate_passed: bool
    gate_reason: str | None = None
    score: float | None = None
    breakdown: dict = field(default_factory=dict)
    premarket_gap_pct: float | None = None
    premarket_volume: float | None = None
    premarket_volume_source: str | None = None
    rvol: float | None = None
    p_continuation: float | None = None
    spread_est: float | None = None
    catalyst_headline: str | None = None
    catalyst_source_url: str | None = None
    catalyst_published_at: str | None = None
    levels: dict = field(default_factory=dict)
    halt_prone: bool = False
    company_name: str | None = None


def _rvol(conn: sqlite3.Connection, ticker: str, premarket_volume: float | None,
          min_history_days: int) -> float | None:
    if premarket_volume is None:
        return None
    rows = conn.execute(
        "SELECT premarket_volume FROM premarket_volume_history WHERE ticker = ? "
        "ORDER BY date DESC LIMIT 20",
        (ticker,),
    ).fetchall()
    if len(rows) < min_history_days:
        return None  # insufficient_history — never guessed
    baseline = sum(r[0] for r in rows) / len(rows)
    if baseline <= 0:
        return None
    return premarket_volume / baseline


def _catalyst_freshness(conn: sqlite3.Connection, ticker: str, half_life_hours: float,
                         now: datetime) -> tuple[float, dict | None]:
    row = conn.execute(
        "SELECT headline, source, source_url, published_at, fetched_at FROM catalysts "
        "WHERE ticker = ? ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if not row:
        return 0.0, None

    headline, source, source_url, published_at, fetched_at = row
    ts_str = published_at or fetched_at
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0.0, {"headline": headline, "source": source, "source_url": source_url,
                      "published_at": published_at}

    age_hours = max((now - ts).total_seconds() / 3600, 0)
    freshness = 0.5 ** (age_hours / half_life_hours)
    return freshness, {"headline": headline, "source": source, "source_url": source_url,
                        "published_at": published_at}


def _key_level_bonus(conn: sqlite3.Connection, ticker: str, premarket_price: float | None) -> tuple[float, float | None]:
    if premarket_price is None:
        return 0.0, None
    row = conn.execute(
        "SELECT MAX(high) FROM daily_bars WHERE ticker = ? "
        "AND date >= date('now', '-20 days')",
        (ticker,),
    ).fetchone()
    trailing_high = row[0] if row else None
    if trailing_high is None:
        return 0.0, None
    return (1.0 if premarket_price > trailing_high else 0.0), trailing_high


def _continuation_probability(rvol: float | None, gap_surprise: float | None, cfg: dict) -> float:
    p = cfg["baseline"]
    if rvol is not None:
        p += cfg["rvol_sensitivity"] * (rvol - 1)
    if gap_surprise is not None:
        p += cfg["gap_surprise_sensitivity"] * (gap_surprise - 1)
    return max(cfg["min"], min(cfg["max"], p))


def score_all(conn: sqlite3.Connection, weights: dict, thresholds: dict) -> list[Candidate]:
    now = datetime.now(timezone.utc)
    candidates: list[Candidate] = []

    for t in resolved_tickers():
        ticker, cluster, recent_ipo = t["ticker"], t["cluster"], t["recent_ipo"]

        quote = conn.execute(
            "SELECT price, prev_close, premarket_price, premarket_gap_pct, premarket_high, premarket_low, "
            "premarket_volume, premarket_volume_source, bid, ask, market_state FROM quotes WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        stats = conn.execute(
            "SELECT atr14, typical_gap_pct, sample_days FROM daily_stats WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        fund = conn.execute(
            "SELECT float_shares, company_name FROM fundamentals WHERE ticker = ?",
            (ticker,),
        ).fetchone()

        if quote is None:
            candidates.append(Candidate(ticker, cluster, recent_ipo, False, "no quote collected"))
            continue

        (price, prev_close, premarket_price, premarket_gap_pct, premarket_high, premarket_low,
         premarket_volume, pm_vol_source, bid, ask, market_state) = quote
        typical_gap_pct = stats[1] if stats else None
        float_shares = fund[0] if fund else None
        company_name = fund[1] if fund else None

        spread_pct = None
        if bid and ask and bid > 0:
            spread_pct = (ask - bid) / bid * 100

        gate_passed, gate_reason = tradability.check(
            {"price": price, "premarket_volume": premarket_volume,
             "spread_pct": spread_pct, "float_shares": float_shares},
            thresholds,
        )

        cand = Candidate(
            ticker=ticker, cluster=cluster, recent_ipo=recent_ipo,
            gate_passed=gate_passed, gate_reason=gate_reason,
            premarket_gap_pct=premarket_gap_pct, premarket_volume=premarket_volume,
            premarket_volume_source=pm_vol_source, spread_est=spread_pct,
            company_name=company_name,
            levels={"premarket_high": premarket_high, "premarket_low": premarket_low, "prior_close": prev_close},
        )

        if recent_ipo:
            # CLAUDE.md: do not let a short window silently produce a
            # confident-looking normalized score. Excluded from ranking.
            cand.breakdown = {"note": "recent_ipo path — insufficient history to normalize, not scored"}
            candidates.append(cand)
            continue

        if not gate_passed:
            candidates.append(cand)
            continue

        gap_surprise = None
        if premarket_gap_pct is not None and typical_gap_pct:
            gap_surprise = abs(premarket_gap_pct) / typical_gap_pct

        rvol = _rvol(conn, ticker, premarket_volume, weights["min_rvol_history_days"])
        cand.rvol = rvol

        catalyst_freshness, catalyst_info = _catalyst_freshness(
            conn, ticker, weights["catalyst_freshness_half_life_hours"], now)
        if catalyst_info:
            cand.catalyst_headline = catalyst_info["headline"]
            cand.catalyst_source_url = catalyst_info["source_url"]
            cand.catalyst_published_at = catalyst_info["published_at"]

        key_level_bonus, trailing_high = _key_level_bonus(conn, ticker, premarket_price)
        cand.levels["trailing_20d_high"] = trailing_high

        float_bonus = 0.0
        fb_cfg = weights["float_bonus"]
        if float_shares is not None and float_shares < fb_cfg["small_float_threshold"]:
            float_bonus = fb_cfg["small_float_bonus"]

        w = weights["weights"]
        score = (
            w["gap_surprise"] * (gap_surprise or 0)
            + w["rvol"] * (rvol or 0)
            + w["catalyst_freshness"] * catalyst_freshness
            + w["key_level_proximity"] * key_level_bonus
            + float_bonus
        )

        cand.score = score
        cand.p_continuation = _continuation_probability(rvol, gap_surprise, weights["continuation_probability"])
        cand.breakdown = {
            "gap_surprise": gap_surprise,
            "typical_gap_pct": typical_gap_pct,
            "rvol": rvol,
            "rvol_insufficient_history": rvol is None,
            "catalyst_freshness": catalyst_freshness,
            "key_level_bonus": key_level_bonus,
            "float_bonus": float_bonus,
            "weights_used": w,
            "formula_version": weights["formula_version"],
        }
        candidates.append(cand)

    return candidates


def select_picks(candidates: list[Candidate], max_picks: int, max_per_cluster: int) -> list[Candidate]:
    scored = sorted(
        (c for c in candidates if c.gate_passed and c.score is not None),
        key=lambda c: c.score, reverse=True,
    )
    cluster_counts: dict[str, int] = {}
    picks: list[Candidate] = []
    for c in scored:
        if cluster_counts.get(c.cluster, 0) >= max_per_cluster:
            continue
        picks.append(c)
        cluster_counts[c.cluster] = cluster_counts.get(c.cluster, 0) + 1
        if len(picks) >= max_picks:
            break
    return picks


def main() -> int:
    weights = load_weights()
    thresholds = tradability.load_thresholds()
    conn = facts_db.connect()
    candidates = score_all(conn, weights, thresholds)
    conn.close()

    passed = [c for c in candidates if c.gate_passed]
    scored = [c for c in candidates if c.score is not None]
    print(f"{len(candidates)} candidates, {len(passed)} passed the tradability gate, "
          f"{len(scored)} scored (excludes recent_ipo)")

    picks = select_picks(candidates, weights["max_picks"], weights["max_per_cluster"])
    print(f"\ntop {len(picks)} picks (max {weights['max_per_cluster']}/cluster):")
    for rank, c in enumerate(picks, 1):
        print(f"  {rank}. {c.ticker:6s} score={c.score:.3f} cluster={c.cluster:20s} "
              f"gap={c.premarket_gap_pct} rvol={c.rvol} p_cont={c.p_continuation:.2f}")

    gated_out = [c for c in candidates if not c.gate_passed and not c.recent_ipo]
    print(f"\n{len(gated_out)} failed the tradability gate:")
    for c in gated_out[:10]:
        print(f"  {c.ticker}: {c.gate_reason}")
    if len(gated_out) > 10:
        print(f"  ... and {len(gated_out) - 10} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
