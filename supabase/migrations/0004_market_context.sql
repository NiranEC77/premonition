-- supabase/migrations/0004_market_context.sql — premonition
--
-- Two additions for the new market-context header, above the picks:
--
--   market_context   — NQ/CL futures level, absolute change, % change, and a
--                       "Your trading rules" flag computed from her own
--                       specified logic (see epicenter/publish_lock.py) —
--                       her discipline displayed back to her, never framed
--                       as premonition's recommendation.
--   macro_headlines  — the top 10 macro/geopolitical headlines, deterministically
--                       filtered toward her watchlist's own clusters (Fed/rates,
--                       oil/energy, semis/export controls, crypto, geopolitical).
--
-- Both jsonb, both on `briefs` (not a new table) — same reasoning as
-- cluster_story: one row per published morning already exists, and this is
-- part of what was actually shown that morning, so it belongs with the
-- brief it was shown alongside, not in a separate always-current table that
-- would drift out of sync with what she actually saw.

alter table briefs add column market_context jsonb;
alter table briefs add column macro_headlines jsonb;
