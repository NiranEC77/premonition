"""publish/health.py — populate ticker_health from universe.yaml.

universe.yaml is the seed for the watchlist and its known landmines (see its
own header comment). This script is the only thing standing between that
seed and the dashboard's /health page — it does not invent any status not
already recorded in universe.yaml. Every ticker not explicitly flagged is
'ok', meaning "the seed's manual audit found nothing wrong," not "a live
collector verified this ticker's data quality" — that distinction matters
and the dashboard says so, because collectors do not exist yet (Phase 2).

Run manually or on a schedule from the agents laptop, never from Vercel:
    python3 -m publish.health --dry-run
    python3 -m publish.health
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from publish import supabase_rest

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO_ROOT / "universe.yaml"

# universe.yaml's flag `status` values are its own vocabulary (recent_ipo,
# needs_confirmation) and don't match ticker_health's check constraint in
# schema.sql ('ok'|'degraded'|'insufficient_history'|'no_data'|'unresolved'|
# 'excluded_tradability'|'restricted'). This is the mapping between the two,
# not a guess — every universe.yaml flag status must appear here or the
# script fails loudly rather than publishing an unmapped status.
_FLAG_STATUS_MAP = {
    "recent_ipo": "insufficient_history",
    "needs_confirmation": "unresolved",
}

# Reason text for the dashboard card. universe.yaml's own `note` fields are
# written for a maintainer reading the YAML, not for the card — these are
# the same facts, phrased for the person looking at /health.
_REASON_OVERRIDES = {
    "CBRS": "CBRS — not a recognized US equity",
    "CIFER": "CIFER — did you mean CIFR?",
    "TE": "TE — did you mean TEL?",
    "ECHO": "ECHO — Echo Global Logistics was taken private; symbol likely dead",
    "SPCX": ("SPCX — IPO'd 2026-06-12, ~1 month of price history; gap distribution, "
             "ATR, and realized vol are undefined"),
    "SNDK": "SNDK — 2025 WDC spinoff, short history; same class of problem as SPCX, smaller",
}

_MISSING_OVERRIDES = {
    "SPCX": ["gap_distribution", "atr", "realized_vol"],
    "SNDK": ["gap_distribution", "atr", "realized_vol"],
}


def load_universe() -> dict:
    return yaml.safe_load(UNIVERSE_PATH.read_text())


def build_rows(universe: dict) -> list[dict]:
    cluster_of: dict[str, str] = {}
    for cluster_name, cluster in universe["clusters"].items():
        for ticker in cluster["tickers"]:
            if ticker in cluster_of:
                raise ValueError(
                    f"{ticker} appears in more than one cluster "
                    f"({cluster_of[ticker]!r} and {cluster_name!r}) — fix universe.yaml"
                )
            cluster_of[ticker] = cluster_name

    flags = universe.get("flags", {})
    now = datetime.now(timezone.utc).isoformat()

    rows = []
    for ticker, cluster in cluster_of.items():
        flag = flags.get(ticker)
        if flag:
            mapped_status = _FLAG_STATUS_MAP.get(flag["status"])
            if mapped_status is None:
                raise ValueError(
                    f"{ticker}: universe.yaml flag status {flag['status']!r} has no "
                    f"mapping to a ticker_health status — add one to _FLAG_STATUS_MAP "
                    f"rather than publishing an unmapped value"
                )
            status = mapped_status
            reason = _REASON_OVERRIDES.get(ticker, flag.get("note", "").strip() or None)
        else:
            status = "ok"
            reason = None
        rows.append({
            "ticker": ticker,
            "status": status,
            "reason": reason,
            "cluster": cluster,
            "missing": _MISSING_OVERRIDES.get(ticker),
            "checked_at": now,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish ticker_health from universe.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Print rows instead of writing to Supabase")
    args = parser.parse_args()

    universe = load_universe()
    rows = build_rows(universe)

    expected_count = universe.get("meta", {}).get("count")
    if expected_count is not None and len(rows) != expected_count:
        raise SystemExit(
            f"universe.yaml's meta.count says {expected_count} tickers but its clusters "
            f"contain {len(rows)} — fix universe.yaml before publishing rather than "
            f"publish a count that contradicts its own seed"
        )

    ok_count = sum(1 for r in rows if r["status"] == "ok")
    print(f"{ok_count} of {len(rows)} healthy")

    if args.dry_run:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    resp = supabase_rest.upsert("ticker_health", rows, on_conflict="ticker")
    print(f"upserted {len(rows)} rows, HTTP {resp.status_code if resp is not None else 'n/a'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
