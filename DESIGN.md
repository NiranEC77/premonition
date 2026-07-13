# premonition

**A pre-market scanner for the opening bell.**

Every trading morning, `premonition` narrows a ~91-name watchlist down to the five names
most likely to make a large, tradable move in the first thirty minutes after the open —
with the catalyst, the key levels, and an honest record of how often it has been right.

---

## 0. Scope, stated plainly

The user is a day trader. Positions are held for minutes, not days. The question is not
"which stock is a good investment" — it is **"which of these 91 names is going to move,
hard, at 09:30, and why."**

What `premonition` does: ranks candidates, explains the catalyst, marks the levels.
What `premonition` does not do: tell anyone whether to take a trade, or how much to risk.
It is a scanner. It hands over evidence, not decisions. That line appears on the dashboard
every day, and it is not decoration — it is the actual boundary of what the system can know.

---

## 1. The target

We forecast the **opening move**, not the daily move.

- **Primary:** magnitude of the move from the open through 09:45–10:00 (the opening range).
- **Secondary:** **continuation vs. fade** — does the gap keep running, or does it fill?

Note what this buys us. By 09:15 the gap has largely already formed in pre-market. We are
not divining the future so much as *ranking what is already visible before anyone looks*.
That is a far more tractable problem than "which stock goes up tomorrow," and it is the
reason this system can work at all.

### Direction, honestly

For the open, "up or down" is nearly tautological — the gap direction is the direction.
The real question, and the hard one, is continuation vs. fade. That is what we forecast,
as a probability, and that is what we get graded on. Heavy pre-market relative volume tends
to continue; thin gaps tend to fill. Whether we can beat that heuristic is exactly what the
scoreboard exists to find out.

---

## 2. Schedule

| Time (ET) | Run | Output |
|---|---|---|
| 22:00 | **Prep** | Catalysts, earnings tomorrow, macro calendar, Asia open, BTC. → ~15-name watch list. |
| 04:30 | **Early** | Europe, overnight news, first gaps. |
| 08:30 | **Draft** | Draft brief published. **08:30 is when US macro prints land** (CPI, PPI, jobs). On those days the index gaps and single-name signal gets swamped — the brief must say so. |
| 09:15 | **THE LOCK** | Final 5, on live gap + pre-market volume. This is the product. |
| 09:15–09:30 | live | Dashboard polls gap and volume until the bell. |
| 16:15 | **Grade** | Score the picks against what the open actually did. |

FOMC days, CPI days, and half-days are flagged, not ignored. A macro day is a different
regime, and the brief says which regime it is in.

---

## 3. Scoring

### 3a. The Premonition score — expected opening move, normalized

The watchlist is a high-beta barbell. QUBT, RGTI, and MARA routinely gap 8% on nothing;
NVDA gapping 4% is a genuine event. Ranking by raw expected move would return the same five
volatile names every single day, and the dashboard would be dead within a week.

So the score is **surprise**, not magnitude:

```
score  ∝  expected opening move / that stock's own typical opening move
```

Raw expected % is still displayed. It just is not what does the ranking.

### 3b. Features, in order of expected weight

1. **Pre-market relative volume (RVOL).** The single best feature. A 6% gap on 3M
   pre-market shares is an event; the same gap on 40k shares is a mirage that fills in four
   minutes. Most retail scanners underweight this. We will not.
2. **Gap size**, normalized against the stock's own gap distribution.
3. **Float and short interest.** Small float + gap + volume is where violent opening moves
   live.
4. **Catalyst freshness.** News that broke at 06:00 is unpriced; news from Friday is priced.
   The timestamp matters more than the headline count.
5. **Catalyst type.** Earnings reaction > unscheduled 8-K > analyst action > press release.
6. **Cluster context.** Is it gapping on its own news, or is the whole complex moving with
   BTC / NVDA / TSM?
7. **Key-level proximity.** Gapping through the prior day's high, a 52-week high, a round
   number.
8. **Options-implied move / IV** — demoted. It prices the whole day, not the open.

### 3c. Tradability gate — enforced BEFORE ranking

A 14% gapper with 30k pre-market shares, a $0.35 spread, and a 4M float is not an
opportunity. It is a trap, and a scanner that surfaces it is actively harmful.

Hard floors: minimum pre-market volume, minimum pre-market dollar volume, maximum spread
(absolute and as a % of price), minimum price. Anything below the floor never appears in a
brief, regardless of how big the gap is. Thresholds live in `tradability.yaml`.

Given this watchlist — QUBT, RGTI, PSIX, POET, EOSE, FCEL, RDW — this gate will fire
regularly. That is the point.

### 3d. Cluster de-duplication — max 2 names per cluster

The 91 names collapse into roughly eight ideas:

| Cluster | Members | Common driver |
|---|---|---|
| Crypto complex | MSTR, COIN, MARA, IREN, WULF, APLD, CIFR, HOOD, CRCL | **BTC price (24/7 — our best free overnight signal)** |
| Quantum | QBTS, QUBT, RGTI | sentiment, single funding headlines |
| Space | SPCX, ASTS, RKLB, LUNR, RDW, GSAT, IRDM | launches, contract awards |
| AI semis / optics | NVDA, AMD, MRVL, CRDO, ALAB, COHR, LITE, AAOI, FN, CIEN, POET, ARM | NVDA + hyperscaler capex |
| Semicap | LRCX, MKSI, NVMI, AEIS, RMBS, TSM | **TSM trades in Taipei overnight — it leads the complex** |
| Memory / storage | MU, WDC, STX, SNDK | DRAM / NAND pricing |
| Power / nuclear | OKLO, BE, EOSE, FCEL, POWL, VRT | datacenter power narrative |
| Mega / software | the rest | idiosyncratic |

Without a cap, a bad night for Bitcoin produces a brief reading MSTR / MARA / WULF / IREN /
CIFR — **one idea wearing five hats.** So: max two per cluster, and when a cluster is hot,
lead with it explicitly: *"Crypto complex is the story: BTC -6% overnight. Best expressions:
MSTR, MARA."* That is a better product than five redundant cards.

Clusters are **derived** (sector + trailing-90d return correlation, recomputed weekly), not
hand-maintained — the watchlist is editable, so the clustering must survive names nobody
mapped in advance.

---

## 4. Data — and the one risk that could sink this

**Latency is the biggest technical risk in the project. Not modeling.**

If free pre-market quotes are 15 minutes stale, the 09:15 lock is worthless and the whole
system is decorative. **Test this in week one, before building anything else.** Take three
tickers, poll every free source through a live pre-market session, and compare timestamps
against a known-good reference.

If free sources cannot deliver live pre-market gap and volume, that is the single place
where a paid tier is genuinely worth the money. Find that out on day three, not week six.

| Signal | Source | Notes |
|---|---|---|
| **Pre-market price + volume** | TBD — **must be latency-validated** | The product depends on this. |
| Daily OHLCV, gap history, ATR | `yfinance` (batched) | Delayed is fine. |
| Float, short interest | `yfinance` info | Sparse. Refresh weekly. |
| Earnings calendar | Finnhub + `yfinance` | Cross-check. Disagreement is a flag, not something to average. |
| **Macro calendar** (CPI / FOMC / jobs) | free econ calendar feed | Regime detector. |
| News, timestamped | Finnhub company-news, per-ticker RSS | **The timestamp is the signal.** |
| **BTC / ETH overnight** | free crypto API | Trades 24/7. Predicts 9 names. Highest value-per-line-of-code in the repo. |
| Overnight world | `ES=F NQ=F ^N225 ^TWII ^GDAXI`, DXY | TSM in Taipei leads the semis complex. |
| Options / IV | `yfinance` option chains | Unofficial, thin on small caps. Degrade gracefully. |
| Deep news reading | Hermes via Brave / Tavily | Shortlist only. |

### Known landmines in this specific watchlist

- **SPCX (SpaceX)** IPO'd on Nasdaq on 2026-06-12 and has already been added to the Nasdaq
  100. It has barely a month of price history, so **every volatility and gap-distribution
  feature is undefined for it.** It needs an explicit `recent_ipo` code path or the model
  will silently emit garbage. Its **lockup expiry (roughly December) is one of the largest
  scheduled catalysts on the entire list** and belongs on the calendar now.
- **SNDK** is a 2025 WDC spinoff — short history, same class of problem, smaller.
- **CIFER / TE / ECHO / CBRS** do not resolve cleanly and need confirming. Best guesses:
  `CIFER` → **CIFR** (Cipher Mining); `TE` → TE Connectivity, which trades as **TEL**;
  `ECHO` (Echo Global Logistics) was taken private; `CBRS` is not a recognized equity.
- Thin or absent option chains across most of the small caps. A missing chain must never
  silently become a zero.

---

## 5. The scoreboard (`aftershock`)

Grading has to match the target. Close-to-close is the wrong metric now.

**Magnitude:** |open → 09:45| for our 5, vs. the universe average, vs. the naive baseline —
*"just pick the 5 biggest pre-market gaps."* **That baseline is the bar.** If the model
cannot beat "sort by gap," everything else in this repo is ornamentation.

**Continuation:** Brier score on the fade/continue call, against a coin flip and against the
plain RVOL heuristic.

**Tradability:** did the picks actually have liquidity at the open, or did the gate leak?

Rolling 30 / 60 / all-time, published on the dashboard, permanently, flattering or not. The
name of this project promises prophecy. The scoreboard is what keeps that promise honest.

---

## 6. Architecture

Deterministic collectors and scorer in Python. Claude Code (headless, driven by systemd) is
the orchestrator: it runs the tools, exercises judgment about which names need a deeper dig,
dispatches Hermes for news reading, validates every number against `facts.sqlite`, and
publishes. **No number in a brief is ever produced by a language model.** See `CLAUDE.md`.

Storage: **git** for code; **SQLite** on the agents laptop as system of record; **Supabase**
for published briefs, picks, grades, and the append-only `watchlist_events` log — so the
universe can be reconstructed as it stood on any past date. Without that, the backtest
quietly lies through survivorship bias.

Future (DL380): deterministic pipeline → **Tanzu CronJob** (stateless, scheduled, restarts
free). Agent orchestrator → **dedicated VM** (stateful, credentialed, and you will want
`tmux` when it misbehaves). Hermes stays on the Spark, where the GPU is.

---

## 7. Phases

1. **Latency test.** Can we get live pre-market gap and volume for free? Answer this first.
   Everything downstream depends on it.
2. **Collectors + `facts.sqlite`.** Deterministic. No LLM, no web.
3. **Backtest gate.** Replay ~120 opens. Does the score beat "sort by gap size"? If not,
   stop and fix the model. Do not build a dashboard on top of no edge.
4. **Scoreboard.** Live, honest, running.
5. **Agent layer.** Claude Code orchestration, Hermes news reading, `verify`.
6. **Dashboard.** Key levels, and the live 09:15–09:30 refresh.
7. **Tune.** Weights move only when the scoreboard says so.
