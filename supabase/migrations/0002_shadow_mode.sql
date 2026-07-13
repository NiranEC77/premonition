-- supabase/migrations/0002_shadow_mode.sql — premonition
--
-- Marks every brief published while the backtest gate (DESIGN.md phase 3)
-- has not yet been passed. Defaults true because that is the actual current
-- state of the project — nobody should have to remember to set this by hand
-- on every publish call for it to be safe. It flips to false only once the
-- scoreboard shows the scorer beating "sort by gap size," per CLAUDE.md's
-- phase gate.

alter table briefs add column shadow_mode boolean not null default true;

-- Phase 2b: six picks, not five (max 2 per cluster still holds — six names
-- must represent at least three distinct ideas). Renaming rather than
-- leaving a "top5" column holding six tickers.
alter table baselines rename column naive_gap_top5 to naive_gap_top6;
alter table baselines rename column naive_rvol_top5 to naive_rvol_top6;
