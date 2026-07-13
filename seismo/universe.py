"""seismo/universe.py — resolve which tickers collectors should actually hit.

Reads universe.yaml (the seed) the same way publish/health.py does. Collectors
skip 'unresolved' tickers (CBRS, TE, ECHO, CIFER as of this writing) entirely
— there is nothing to fetch for a symbol that doesn't exist — but still
collect for 'insufficient_history' tickers (SPCX, SNDK) since they resolve
fine, they just take the recent_ipo scoring path downstream.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO_ROOT / "universe.yaml"

_FLAG_STATUS_MAP = {
    "recent_ipo": "insufficient_history",
    "needs_confirmation": "unresolved",
}


def load_universe() -> dict:
    return yaml.safe_load(UNIVERSE_PATH.read_text())


def resolved_tickers() -> list[dict]:
    """Returns [{ticker, cluster, recent_ipo: bool}] for every ticker collectors
    should actually fetch — everything except 'unresolved' symbols."""
    universe = load_universe()
    cluster_of: dict[str, str] = {}
    for cluster_name, cluster in universe["clusters"].items():
        for ticker in cluster["tickers"]:
            cluster_of[ticker] = cluster_name

    flags = universe.get("flags", {})
    out = []
    for ticker, cluster in cluster_of.items():
        flag = flags.get(ticker)
        status = _FLAG_STATUS_MAP.get(flag["status"]) if flag else "ok"
        if status == "unresolved":
            continue
        out.append({
            "ticker": ticker,
            "cluster": cluster,
            "recent_ipo": status == "insufficient_history",
        })
    return out
