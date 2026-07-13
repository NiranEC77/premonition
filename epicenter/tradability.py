"""epicenter/tradability.py — the gate. Enforced before ranking, never after.

CLAUDE.md's tradability gate, read from tradability.yaml. A ticker with no
usable premarket volume figure fails the gate as INSUFFICIENT_DATA — it is
never assumed tradable just because we couldn't check.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TRADABILITY_PATH = REPO_ROOT / "tradability.yaml"


def load_thresholds() -> dict:
    return yaml.safe_load(TRADABILITY_PATH.read_text())


def check(ticker_facts: dict, thresholds: dict) -> tuple[bool, str | None]:
    """ticker_facts: {price, premarket_volume, spread_pct, float_shares}
    (any of which may be None). Returns (passes, reason_if_not)."""
    price = ticker_facts.get("price")
    premarket_volume = ticker_facts.get("premarket_volume")
    spread_pct = ticker_facts.get("spread_pct")
    float_shares = ticker_facts.get("float_shares")

    if price is None or premarket_volume is None:
        return False, "insufficient_data: missing price or pre-market volume"

    if price < thresholds["min_price"]:
        return False, f"price ${price:.2f} below floor ${thresholds['min_price']:.2f}"

    dollar_volume = price * premarket_volume
    if dollar_volume < thresholds["min_premarket_dollar_volume"]:
        return False, (f"pre-market dollar volume ${dollar_volume:,.0f} below floor "
                        f"${thresholds['min_premarket_dollar_volume']:,.0f}")

    if spread_pct is not None and spread_pct > thresholds["max_spread_pct"]:
        return False, f"spread {spread_pct:.2f}% above floor {thresholds['max_spread_pct']:.2f}%"

    if float_shares is not None and float_shares < thresholds["min_float_shares"]:
        return False, f"float {float_shares:,.0f} below floor {thresholds['min_float_shares']:,.0f}"

    return True, None
