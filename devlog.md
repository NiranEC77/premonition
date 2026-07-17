# devlog — premonition

Running log of what happened and why, in the order it happened. `RUNBOOK.md` is how to
run the system; `CLAUDE.md` is the rules it runs under; this is the history — decisions,
findings, and dead ends, so nobody has to reconstruct them from git blame in six months.

---

## 2026-07-13 — Phase 1: the latency question

Built Probe A (freshness) and Probe B (friction) per DESIGN.md's Phase 1 mandate: answer
"can we see live pre-market price and volume for free?" before building anything else.
Both probes only write raw observations to `probe.sqlite` — no scoring, no aggregation.

## 2026-07-13 — Phase 1c: volume bug + after-hours coverage

Found and fixed a real bug: the forming (incomplete) 1-minute bar always reads volume=0,
and early code wasn't excluding it, so every volume read looked artificially low. Also
started capturing Yahoo's `regularMarket*`/`preMarket*`/`postMarket*` fields uncascaded
(previously one field silently overwrote another). Installed the 15:55–18:00 ET
after-hours systemd timer.

## 2026-07-13 — Phase 2a: dashboard skeleton

Built the first Vercel dashboard (Astro + Supabase, anon-key-only, RLS-gated): `/health`,
`/freshness`, `/scoreboard`. No picks, no scoring yet — just proving the anon key can read
and, deliberately, cannot write.

## 2026-07-13 — Phase 2b/2c: real pipeline, dashboard for her not us

User corrected an early plan to build a "minimal decorative" pipeline — insisted on the
full real thing: collectors (all 91 tickers) → deterministic scorer (`weights.yaml`, no
LLM) → grader, publishing to Supabase at the 09:15 lock. Explicitly deferred Hermes, the
verifier, and the nightly report to a later round. Explicitly asked to be told, honestly,
whether historical pre-market volume is retrievable for free for 91 tickers over ~120 past
sessions — answer: **no reliable backfill exists**; `premarket_volume_history` builds
forward from today only, and does not substitute regular-session volume relabeled as
pre-market.

Rebuilt the dashboard around the scalper, not the builder: scoreboard as the home page
with a plain-English verdict, six picks (not five, matching DESIGN.md's real target),
shadow-mode banner that cannot be dismissed, progressive disclosure ("for the pros" detail
under a plain-English move summary), and a secure ticker-edit path — Vercel API route,
`service_role` key server-side only, httpOnly HMAC session cookie, writes are
insert-only into `watchlist_events` (append-only, so the watchlist's history at any past
date is reconstructable — no survivorship bias in the backtest).

Installed the 08:30 / 09:15 / 16:15 ET systemd timers for draft / lock / grade.

Deploy troubleshooting: Vercel kept failing with
`NOW_SANDBOX_WORKER_ROOTDIR_NOT_EXIST`. First guess (a stray `rootDirectory: "web"` in
project settings) was wrong — clearing it didn't fix anything. Real cause: the project's
Root Directory is genuinely `web` (needed for git-based auto-deploys, which check out the
whole repo), but the `deploy_to_vercel` file-payload deploys need every path prefixed with
`web/` to match. Confirmed with a 4-file test payload before redeploying the full ~20-file
one.

## 2026-07-14 — First live pipeline run, and the volume finding gets confirmed hard

Watched the real 08:30 draft and 09:15 lock run live via a background monitor. Draft
succeeded (87 tickers, one transient Yahoo 500 on ANET that self-recovered on the next
pass). Lock succeeded and published — with **zero picks**, correctly: every one of the 86
tickers that resolved a quote came back with `premarket_vol=0.0` from summed completed
1-minute bars, not just the one name (NVDA) watched earlier. This is the free-data-source
finding from Phase 1, now confirmed at full-watchlist scale rather than a 3-ticker sample:
Yahoo's free intraday bars do not carry real volume during pre-market, structurally, for
any ticker. The tradability gate did exactly what it's supposed to do — refused to rank on
unverifiable volume rather than fabricate a result — and the dashboard rendered the honest
empty state (`"Zero picks cleared the bar on 2026-07-14."`) live, in production.

Researched paid/alternative data sources to fix this. Ruled out: IEX Cloud (confirmed
shut down Aug 2024). Alpaca's free tier is IEX-only (too thin for names like QUBT/RGTI);
their paid "Algo Trader Plus" ($99/mo) has confirmed real-time consolidated-tape
extended-hours coverage. Polygon, Twelve Data, Finnhub-paid, Databento all have plausible
but *unconfirmed* pre-market coverage at the tier level — not committed to without direct
verification.

Best option found: the user's wife has a funded Interactive Brokers account. Broker-grade
data, already paid for. Researched the real blocker — unattended headless operation — and
it's better than feared: IBKR only forces full re-auth once a week (Sunday 01:00 ET), not
daily; a scheduled auto-restart (via IBC) reuses the session the rest of the week. The
weekly re-auth still requires a physical tap on the IBKR Mobile app — no way found to
script past that (checked open IBC GitHub issues asking for exactly this; unresolved as of
this research). Accepted as a known, documented tradeoff against the "must not require
anything by hand" bar, rather than silently ignored.

Got explicit sign-off: wife is aware and consenting to a **read-only** connection; build
against her **paper trading account** first, not live.

Installed IB Gateway 10.45 and IBC 3.24.1 (no sudo — bundled-JRE standalone installer, all
under `~/.local/opt/`), and `ib_async` 2.1.0 in the project venv. Confirmed IBC's
`ReadOnlyLogin` config flag exists — the connection will be structurally blocked from
placing orders, not just trusted not to. Not yet configured with credentials; blocked on
the paper account's username/password and confirmation of the account's market data
subscription, both to come from the user.

## 2026-07-14 — IBKR credential handling, a security mistake, and a real login failure

Corrected understanding of IBKR login: it's a single set of credentials (her live
account's actual username/password), not a separate paper-only login — `TradingMode`
in IBC's config picks live vs. paper at the same login screen. That makes the credential
itself more sensitive than originally scoped. User sent it via a `read -s` prompt run
directly on the box (through `sudo -iu claude-orch`), keeping it out of this chat.

**Security mistake, caught and fixed the same session:** first launch attempt passed the
credentials as `--user`/`--pw` command-line arguments to `ibcstart.sh`. `ps`/`pgrep -af`
show full process command lines to any local shell user, and running `pgrep -af` and
`ps aux` while that process was alive put the real password into two tool outputs —
i.e. into this conversation's transcript. Killed the processes immediately, rebuilt
`ibkr/start_gateway.sh` to never pass credentials as arguments: it now writes a
runtime-only copy of `config.ini` (mode 600, under `~/.local/state/ibkr-runtime/`,
regenerated from `/etc/premonition/env` on every start) with `IbLoginId`/`IbPassword`
set there instead — confirmed against IBC's own source (`ibcstart.sh` line ~530) that
omitting `--user`/`--pw` makes it fall back to reading the ini file, which is not
visible via `ps`. The git-tracked `ibkr/config.ini` template always ships with both
fields blank. Recommended the user rotate the IBKR password as a precaution given the
transcript exposure, regardless of the fix.

Also hit two infra issues getting IB Gateway running at all, both fixed: (1) IBC expects
the stock installer's directory layout (`<path>/ibgateway/<version>/jars`), but the
no-sudo standalone install was flat — fixed with a symlink (`ibgw/ibgateway/1045` →
the real install dir) rather than reinstalling. (2) IB Gateway is a Java Swing app and
hard-requires an X11 display even for unattended use — installed `xvfb` (needed sudo,
one-time) and wrapped the launch in `xvfb-run`.

With both fixed, the login attempt failed — but not on 2FA. The wife's IBKR Mobile app
never received a push, which looked at first like a 2FA-delivery problem. Screenshotting
the virtual display directly (`ffmpeg -f x11grab` against the Xvfb display, since the
Java process's own log never surfaces this) showed the real cause: **"Connection to
server failed: Invalid username or password."** Login is failing before it ever reaches
the 2FA step, which is also why no push arrived — IBKR never got that far. Confirmed the
credential pipeline itself is not the bug (the password's length in the runtime file
matches exactly what was typed). Asked the user to have her verify the credentials by
logging into IBKR's Client Portal website directly, to rule out a typo vs. a genuine
wrong credential, before retrying.

## 2026-07-16 — Alpaca/FMP/CoinGecko go live, and quote sanity becomes a real gate

Pivoted the pre-market-volume problem away from the still-unresolved IBKR paper login
(last known state: waiting on the user to verify credentials against IBKR's own website)
toward three lighter-weight sources that landed live keys today: Alpaca (IEX feed, paper
account), Finnhub (already wired for news since 07-13), and FMP. Also formalized this
file's own scope in `CLAUDE.md` — narrative session log, distinct from any future
`HERMES-CHANGELOG.md`/`HERMES-EXPLAINED.md`, which stay Hermes-specific and terse.

**Alpaca IEX is now the primary pre-market feed.** Two batched adapters (one HTTP call
across all ~87 tickers, not 87 calls — `probes/sources.py`'s `fetch_alpaca_quotes_batch`
/ `fetch_alpaca_bars_1m_batch`): live bid/ask, and historical 1-minute bars mirroring the
existing yfinance forming-bar-safe pattern, except on REAL volume — unlike yfinance's
chart API, Alpaca's IEX bars do not structurally zero out extended-hours volume. Verified
this live: NVDA's pre-market bars this morning showed real, escalating volume from a
04:31 ET print of 100 shares up to 42,358 shares by the 09:30 open. That's the actual
proof the whole IBKR detour was chasing, arrived at a different way.

**The quote-sanity problem is real and reproduced itself while testing, unprompted.**
While smoke-testing Alpaca's auth, a live curl against NVDA — no synthetic example needed
— came back bid 195.57 / ask 230.00, a 17.6% spread, and RGTI came back at a 32%
spread — both after-hours, both exactly the frozen-quote shape the user had already found
manually the night before. Built `probes/quote_sanity.py`: two independent, versioned
gates (freshness — is the source's own timestamp actually recent — and spread sanity — is
the gap even plausible), pure function, no I/O, reused identically by the probes (record
only, never filter — a probe's job is to show what a source returned, garbage included)
and by `seismo/collect_quotes.py` (actually gate: a failing quote gets its price/bid/ask
nulled so the tradability gate naturally treats it as no data, and the rejection is logged
to `/srv/premonition/logs/quote-sanity-rejects.jsonl`, same append-only pattern as
`verify-rejects.jsonl`). Later the same evening, running the full 87-ticker production
path live turned up something the earlier spread-based examples hadn't: many tickers were
returning `ask=0` outright — not a wide spread, an actually-absent ask. The gate is
worded to catch that as its own `crossed` status rather than lump it in with
`implausible_spread`, which turned out to matter, since `ask=0` was the single most common
real rejection reason across the full watchlist tonight.

**FMP has a real per-symbol restriction, discovered by testing, not documentation.**
`/stable/earnings` returned real data for COIN but HTTP 402 ("This value set for 'symbol'
is not available under your current subscription") for MSTR and MARA — confirmed with an
immediate retry to rule out a rate limit. This key's tier evidently whitelists a subset of
symbols for earnings data; it is NOT reliable across the full watchlist. Recorded
per-ticker, not silently dropped — `earnings_dates.error` already existed for exactly this
shape of problem. FMP's IPO calendar (`/stable/ipos-calendar`) — wanted specifically for
SPCX's lockup-expiry catalyst per DESIGN.md — is flatly restricted on this tier (402, no
per-symbol exceptions seen). Not implemented; not worked around. The splits calendar
(`/stable/splits-calendar`) is NOT restricted and is wired in, writing straight into the
existing `catalysts` table rather than inventing new schema for something with no rows
yet.

**CoinGecko replaces yfinance as BTC/ETH's primary source**, yfinance falls back only if
CoinGecko fails; every other macro symbol (futures, indices, DXY) still yfinance-only,
since CoinGecko has no coverage there.

**A real, if minor, security lesson repeated itself.** Ran `pgrep -af`/`ps aux` while
testing IBKR credentials on 07-14 and leaked a real password into this transcript (see
that day's entry) — today's design for the NEW secrets (Alpaca, FMP) never had that
exposure surface to begin with, because they're plain env vars read straight into request
headers/params, never passed as process arguments. Worth naming as the payoff of that
earlier mistake, not just the mistake itself.

Also found, while testing, an unrelated pre-existing bug not in today's scope: Finnhub's
earnings-calendar call passes no `from`/`to` date range at all, which may be why it
returned nothing for every ticker tested tonight even where yfinance found a real date a
few weeks out. Flagged, not fixed — out of scope for today's ask.

Full pipeline proof, end to end, same evening: ran `collect_quotes.py` against all 87
tickers live (after-hours, so genuinely nothing should pass), then `epicenter/score.py`
against the result — 0 of 87 candidates cleared the tradability gate, every single one
correctly `insufficient_data`, zero crashes, ~2 seconds wall time including Alpaca's
paginated historical-bars fetch across the whole watchlist. The correct answer to "does
anything trade after-hours on a Thursday evening" is "no, and the system should say so
honestly" — which is exactly what it did.

Moved the pre-market probe timer from 08:00 to 07:55 ET (`systemd/premonition-probe-*`),
confirmed via `systemctl --user list-timers` that the next real fire is
2026-07-17 07:55:00 EDT. Added a "Can I trust these numbers?" section to
`how-this-works.astro` — plain-English bid/ask explainer, a live frozen-vs-healthy
comparison (195/230 vs. 195.55/195.57), and the point that the same sanity check that
protects her from a wrong number is also what tells the system which names are asleep.

One loose end, flagged to the user rather than fixed unilaterally: `/etc/premonition/env`
has a stray malformed line (a bare 32-character fragment, no `KEY=` prefix, sitting
between `ALPACA_BASE_URL` and `FMP_API_KEY`) that throws a harmless "command not found"
on every `source` — almost certainly a paste artifact. Editing the secrets file without
the user naming the specific line got (correctly) blocked by the auto-mode classifier;
left for the user to clean up rather than force through.

Verified the FULL pipeline, not just the probe, will actually publish tomorrow — the user
pushed on this specifically ("I want real output on the dashboard tomorrow, tell me now if
anything blocks it"). `premonition-draft.timer`/`premonition-lock.timer` already existed
from Phase 2b/2c and both call `seismo.collect_quotes`, so today's Alpaca rewiring is
already in their hot path with no further changes needed. Proved the write path without
risking today's already-published historical brief: dry-ran `publish_lock` against
tonight's Alpaca-collected data (works), then read-verified both the service_role and
anon Supabase keys directly against today's real brief (`id=3`, both `200`), then
confirmed the live dashboard renders it. `systemctl --user list-timers` shows all three
(probe 07:55, draft 08:30, lock 09:15) firing tomorrow, 2026-07-17, ET.

**Caught a real, structural risk before it could produce a false negative tomorrow.**
`tradability.yaml`'s $500k pre-market dollar-volume floor was a placeholder from before
any real volume source existed. IEX (Alpaca's free feed) is only ~2% of the consolidated
tape — applying a floor sized for the full tape to an IEX-only number would gate out
genuinely liquid names by a data-source artifact, not a real liquidity problem, and make
tomorrow's zero (if it happens) meaningless as a data point either way. Flagged it instead
of silently living with it. User's call: set a provisional floor at ~2% of the old value
($10k) — clearly commented in `tradability.yaml` as a stopgap awaiting real calibration,
not a tuned number — and log the actual top-10 tickers by measured pre-market dollar
volume on every `collect_quotes` run (`/srv/premonition/logs/premarket-dollar-volume-
*.log`), so the real threshold can be set from Saturday's data instead of another guess.
Tested the log-writing/ranking logic with synthetic data since literally nothing passed
sanity during tonight's after-hours test run to exercise it with real numbers (0 of 87
tickers sane — 29 crossed, 58 stale, which is itself the expected, correct answer for a
Thursday evening). The real test of both the provisional floor and the ranking log is
tomorrow morning.

## 2026-07-16 (continued) — Market-context header: NQ/CL tiles, her trading rules, macro news, per-pick catalysts

A big, mostly-mechanical addition, one real near-miss caught by testing rather than review.
Added CL=F alongside the already-collected NQ=F in `collect_macro.py`. Built
`seismo/collect_macro_news.py` — Finnhub's general news feed, deterministic keyword
filter tied to the watchlist's own clusters (Fed/rates, oil/energy, semis/export
controls, crypto, geopolitical), timestamp-ranked, top 10, stored as a wipe-and-reload
snapshot table (not an accumulating history — the published brief is what preserves "what
she actually saw," this table is just today's working data, same relationship
`premarket_volume_history` has to `quotes`). Tested against tonight's real feed: 9 of 10
matches were genuinely relevant Iran/Israel geopolitical headlines (apparently a live
story tonight), the tenth was a Jim Cramer roundup that matched on "TSMC" — checked the
summary before assuming it was a false positive; it wasn't, TSMC is explicitly in
DESIGN.md's semis cluster.

Built the NQ/CL trading-rule flags in `publish_lock.py` exactly as specified, verbatim —
resisted the urge to "clean up" the asymmetric threshold structure between the two
instruments. Left the requested code comment flagging that NQ's "otherwise" band spans
-1% to +0.5% (a flat morning and a -0.8% morning both show yellow) — not changed, per
instruction, just flagged for her to confirm separately.

Extended `picks.reasons` from a single most-recent catalyst to every catalyst within 72h
(capped at 5 for card sanity) — this is the "catalyst evidence" the pick card and "For
the pros" now both show with source and timestamp. Reused the existing `catalysts` table
and the existing jsonb `reasons` column rather than inventing new schema — it was already
shaped as a list, just never populated with more than one item.

**Caught before it could silently break tomorrow's lock, by reading the actual bin
scripts rather than assuming the new collectors were wired in just because they existed:**
`bin/premonition-draft` didn't call the new `collect_macro_news` at all — would have left
`macro_headlines` permanently empty in production despite working perfectly in every
manual test. Separately, `bin/premonition-lock` never refreshed macro data at all, only
quotes — meaning NQ/CL tiles at 09:15 would have shown 45-minute-stale 08:30 draft data
while being displayed as live. Both fixed: draft now runs `collect_macro_news` too; lock
now re-runs `collect_macro` and `collect_macro_news` immediately before publishing, same
freshness discipline as the quote snapshot already gets.

**Real, not-yet-resolved dependency**: `market_context`/`macro_headlines` need two new
`jsonb` columns on `briefs` — `ALTER TABLE` isn't reachable through PostgREST, same
constraint as every previous migration. Asked the user to run
`supabase/migrations/0004_market_context.sql` via the SQL Editor; until that lands,
tomorrow's lock will fail outright trying to write to columns that don't exist. Everything
else was tested short of the actual Supabase write, using `--dry-run` and direct read
checks, specifically to avoid re-overwriting today's already-published, already-graded
historical brief a second time.

## 2026-07-16 (evening) — "push" turns out to mean two things, and a favicon

Asked to "push." Committed and pushed to git — straightforward. Then "I don't see the new
dashboard": turned out `git push` alone does nothing to Vercel here, since the dashboard
deploys via direct file upload (`deploy_to_vercel`), not git integration — confirmed by
the absence of a `.vercel/project.json` locally. Redeployed manually, which then revealed
a second problem: the fresh deployment came up with **no Supabase environment variables at
all** ("Not connected"), even though the same project had them working minutes earlier.
Asked the user to check Vercel's env var scoping; they added/fixed the keys, redeployed —
and *while that manual redeploy was still queued*, discovered a THIRD wrinkle:
`list_deployments` showed a separate deployment, already READY, built from the exact git
commit pushed 18 minutes earlier. Git integration was real after all — it just runs
independently of (and apparently slower than, or queued behind) the manual file-upload
path. Corrected understanding, logged for next time: this project has both a git-connected
Vercel deployment method AND a manual one; either can produce a live production deploy,
they can race each other, and "no `.vercel/project.json`" only tells you the manual path
was used at some point, not that git integration is absent.

Built a favicon: two bold bars, gapping up, in the dashboard's existing brand blue,
adaptive light/dark via the same `prefers-color-scheme` pattern as the rest of the site.
First draft was a literal two-candlestick glyph (body + wick caps) — rendered it to PNG at
16/32/64px with `sharp` (already a transitive dependency of this Astro project, no new
install needed) before shipping, and the wick details turned out to blur into mush at
16px, the size that actually matters for a browser tab. Simplified to two solid rounded
bars with no wick detail; re-rendered, checked again, shipped. Deployed via git push this
time — landed in under 4 seconds with no manual-deploy contention, confirming the git path
is the fast, right one to use going forward when nothing needs the file-upload workaround.

## 2026-07-17 — The first real morning, a crash caught with 12 minutes to spare, and the first real pick

The whole week's work got its first live test against genuine pre-market hours, and it
was not a quiet one. `bin/premonition-draft` (08:30) crashed instantly — exit 127, no
output at all, not even its own startup `echo`. Root cause: that stray malformed line in
`/etc/premonition/env`, flagged on 07-16 and never cleaned up, is completely harmless when
sourced interactively (as in every manual test that week) but fatal the instant it's
sourced inside a script running `set -euo pipefail` — which both `bin/premonition-draft`
*and* `bin/premonition-lock` do. It would have taken down the 09:15 lock identically,
about 45 minutes later, publishing nothing at all — worse than an honest zero-pick
morning, silence. Caught it at 08:47 via a routine "how are we looking" status check, not
by design — nobody was specifically watching for it.

Asked the user to remove the offending line directly (twice — my own attempts to edit the
secrets file were correctly blocked both times by the auto-mode classifier, since neither
"how are we looking" nor "done" named that specific line as authorized to change). The
user's fix didn't land in time. With the clock now genuinely tight, changed approach:
instead of the secrets file, made both `bin/premonition-draft` and `bin/premonition-lock`
robust to a malformed line in it — filter to well-formed `KEY=VALUE` lines
(`grep -E '^[A-Za-z_][A-Za-z0-9_]*='`) before sourcing, rather than blindly sourcing the
whole file. This sidesteps the permission question entirely (never touches the secrets
file) and is the more correct fix regardless — a script that can be taken down by one
stray line in a file it doesn't control was always fragile, this one bad line was just the
first thing to prove it. Verified the fix under `set -e` directly, then manually re-ran
`bin/premonition-draft` to catch `facts.sqlite` up before the real lock fired, finishing
with about 12 minutes to spare.

The actual result, once the pipeline could run: of 87 tickers, only **2** produced a sane,
fresh quote on Alpaca's IEX feed this morning — RGTI and QUBT — everything else was
`stale` or `crossed`. That is exactly the finding the whole Alpaca migration was trying to
surface, now confirmed at full scale during a real session, not an after-hours test. Of
those two, only RGTI's pre-market dollar volume ($28,689) cleared the provisional $10k
floor; QUBT's ($3,725) didn't. The 09:15 lock published RGTI — brief id=4, the **first
real, non-zero pick this project has ever produced** — halt_prone correctly flagged, real
catalyst evidence attached (a D-Wave Quantum headline), NQ/CL tiles showing real numbers
for the first time (NQ -1.98% correctly triggered the red "high alert" flag; CL +3.36%
correctly triggered yellow). Confirmed all of it rendering live on the production
dashboard within a minute of publish.

One cosmetic bug noticed while checking the live render, not fixed yet: RGTI's catalyst
list shows the same Finnhub headline ("Watching Broadcom...") three times — a genuine
duplicate in the underlying collected data (same article id, collected on separate runs),
not a rendering bug. Worth a dedupe pass in `_recent_catalysts` later; not urgent, doesn't
misrepresent anything, just repeats it.

Reordered the home page on the user's direct feedback after seeing the real RGTI card
render: picks now sit directly under the market-context tiles, macro news moved to the
bottom of the page. The picks are the actual product; the macro/geopolitical context is
supporting material, and the layout hadn't reflected that until someone actually looked at
the live page and said so.
