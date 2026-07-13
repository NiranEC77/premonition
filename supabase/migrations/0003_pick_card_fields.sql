-- supabase/migrations/0003_pick_card_fields.sql — premonition
--
-- Two additions for the pick card's "For the pros" section: the score
-- breakdown (each feature's contribution, so a reader can see exactly why a
-- number is what it is, not just the final score) and the company name
-- (plain-language, sits next to the ticker on the card).

alter table picks add column score_breakdown jsonb;
alter table picks add column company_name text;
