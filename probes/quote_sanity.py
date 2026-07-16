"""probes/quote_sanity.py — is a quote real, or a frozen/garbage leftover?

Two independent gates, both must pass before a quote's bid/ask/price is
trustworthy enough to publish as a number:

  1. Freshness — is the source's OWN timestamp actually recent, or a stale
     leftover from whenever the name last printed? A "successful" HTTP 200
     response with a real-looking bid/ask can still be hours old on a
     sleeping name.
  2. Spread sanity — is the bid/ask gap even plausible? A frozen bid sitting
     next to a stale/nonsense ask (or vice versa) commonly shows up as an
     absurdly wide spread — real example, seen live on Alpaca IEX after
     hours: NVDA bid 195.57 / ask 230 (17.9% spread), RGTI bid 12.13 / ask
     16.03 (32.1% spread). Neither is a real, tradable market; both are what
     a sleeping name's last-known quote looks like once nobody is making one.

Distinct from tradability.yaml's max_spread_pct, which is a business
decision about what's tradeable — this is a data-quality decision about
what's even a real quote in the first place. The sanity ceiling here is
deliberately much looser than the tradability floor: something can be
sane-but-untradeable (wide but real), but nothing gets ranked or scored on
something that isn't a real quote at all.

A quote that fails either gate isn't degraded and passed through — it's
flagged, and the CALLER decides what to do with that. A probe records the
rejection (this module never touches probe.sqlite or facts.sqlite directly —
no I/O anywhere in here); seismo/collect_quotes.py nulls the field so the
tradability gate naturally treats it as no data, and logs the rejection to
/srv/premonition/logs/quote-sanity-rejects.jsonl, the same pattern as
CLAUDE.md's verify-rejects.jsonl.
"""

from __future__ import annotations

from datetime import datetime, timezone

QUOTE_SANITY_FORMULA = "freshness_and_spread_v1"

MAX_QUOTE_AGE_SECS = 90     # matches CLAUDE.md's 09:15-lock freshness bar
MAX_SANE_SPREAD_PCT = 10.0  # far looser than tradability.yaml's 1.5% trading floor —
                             # this catches "not a real quote," not "too wide to trade"


def check_quote_sanity(bid: float | None, ask: float | None, source_ts: str | None,
                        fetched_at: str | None) -> dict:
    """Pure function, no I/O, no side effects. Returns:
      {
        sanity_status: 'ok' | 'no_quote' | 'crossed' | 'stale' | 'implausible_spread',
        sanity_reason: str | None,
        quote_age_secs: float | None,
        spread_width: float | None,   # ask - bid, in price units
        spread_pct: float | None,     # spread_width / bid * 100
        quote_sanity_formula: str,    # versioned, travels with every result
      }
    A quote missing bid or ask outright is 'no_quote' — a distinct status
    from 'stale', since "the source has nothing" and "the source has
    something untrustworthy" are different facts and should never be
    conflated."""
    result = {
        "sanity_status": "ok",
        "sanity_reason": None,
        "quote_age_secs": None,
        "spread_width": None,
        "spread_pct": None,
        "quote_sanity_formula": QUOTE_SANITY_FORMULA,
    }

    if bid is None or ask is None:
        result["sanity_status"] = "no_quote"
        result["sanity_reason"] = "missing bid or ask"
        return result

    if source_ts and fetched_at:
        try:
            src_dt = datetime.fromisoformat(source_ts)
            now_dt = datetime.fromisoformat(fetched_at)
            if src_dt.tzinfo is None:
                src_dt = src_dt.replace(tzinfo=timezone.utc)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            result["quote_age_secs"] = (now_dt - src_dt).total_seconds()
        except (TypeError, ValueError):
            result["quote_age_secs"] = None

    if bid <= 0 or ask <= 0:
        result["sanity_status"] = "crossed"
        result["sanity_reason"] = f"non-positive bid/ask: bid={bid} ask={ask}"
        return result

    if bid > ask:
        result["sanity_status"] = "crossed"
        result["sanity_reason"] = f"bid ({bid}) above ask ({ask}) — inverted market, not real"
        return result

    result["spread_width"] = ask - bid
    result["spread_pct"] = (ask - bid) / bid * 100

    if result["quote_age_secs"] is not None and result["quote_age_secs"] > MAX_QUOTE_AGE_SECS:
        result["sanity_status"] = "stale"
        result["sanity_reason"] = (f"quote is {result['quote_age_secs']:.0f}s old, older than "
                                    f"the {MAX_QUOTE_AGE_SECS}s freshness bar")
        return result

    if result["spread_pct"] > MAX_SANE_SPREAD_PCT:
        result["sanity_status"] = "implausible_spread"
        result["sanity_reason"] = (f"spread {result['spread_pct']:.1f}% wider than the "
                                    f"{MAX_SANE_SPREAD_PCT:.0f}% sanity ceiling — almost "
                                    f"certainly a frozen/stale quote, not a real market")
        return result

    return result
