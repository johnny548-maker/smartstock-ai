# ADR 2026-06-13 — SmartStock leadership-signal weight re-gate (15y / 661-name universe)

> Status: **Accepted** · Supersedes the 82-name (2024) leadership weights · Branch: `auto-optimize/20260606-0213`
> Reconstructed 2026-06-14 from the canonical rationale in `config.py` `LEAD_*` (this file was referenced by `config.py`/`strategy.py` but had never been committed — a ghost ADR).

## Context

Leadership-pattern signal weights (`config.LEAD_*`) feed `strategy.score_stock` additively. The original weights were fitted on an **82-name universe** (later re-confirmed on the committed **65-ticker** `backtest_15y_hardened.txt`). At that scale several signals showed huge lift and were given large weights — most notably **首次新高 (first-new-high)** which held the **largest weight (15)**.

A signal "lift" is precision / base-rate of catching a +25% explosive move over a 60-bar horizon. The keep/kill gate is **Wilson-CI lower bound > base rate**, plus Bonferroni + Benjamini-Hochberg multiple-testing correction, net-of-cost (next-open fill + 15 bps slippage + 30 bps fee). Base rate ≈ 6.99% on the 65-ticker set.

The concern: with only ~47 fired events for 首次新高, that 2.44 lift is a **small-sample mirage**. We re-ran on a **661-name universe** (0050 + 中型100 + S&P500, sanitised, 15y) with the full multiple-testing family.

## Evidence — the overfit collapse

The 65-ticker "before" run (`backtest_15y_hardened.txt`, base rate 6.99%) vs the 661-name re-gate:

| Signal | 82/65-univ lift | 661-univ lift | n (661) | 661 verdict |
|--------|----------------:|--------------:|--------:|-------------|
| 首次新高 (first new high) | **2.44** (n≈47, flat-regime 2.95, down-regime 21.63!) | **0.68** | ≈696 | **KILL** — worse than random |
| Power pivot (放量突破) | 2.04 | 1.24 | — | KILL (p≈0.05) |
| Trend Template / Stage-2 | 1.36 | 1.00 | — | KILL |
| Pocket pivot | 1.35 | 0.99 | — | KILL |
| RS線新高 (純) | 1.23 | 0.99 | — | KILL |
| U/D 量比吸籌 | 1.39 | **1.55** | ≈47653 | **KEEP** — PASS CI/Bonf/BH |
| VDU→Thrust (量縮噴出) | 1.46 (CI-fail) | **1.61** | ≈1466 | **KEEP** — newly earns weight |

The smoking gun: 首次新高 n=47→lift 2.44 on 82-univ, n=696→lift 0.68 on 661-univ. The signal with the **most extreme small-sample lift held the largest weight**, and on a credible universe it is **worse than a coin flip**. The down-regime 21.63 lift on n≈small is itself a tell of n→0 instability.

## Decision

`config.LEAD_*` realigned to the 661-name gate (only CI-lower > base AND Bonferroni AND BH survivors earn weight):

```
LEAD_UD_ACCUM      = 8    # KEEP — lift 1.55, n≈47653 (keyless, works on US)
LEAD_VDU_THRUST    = 10   # KEEP — lift 1.61, promoted (was CI-fail on 82-univ)
LEAD_FIRST_NEW_HIGH= 0    # KILL — lift 0.68 (was the 15-pt overfit champion)
LEAD_POWER_PIVOT   = 0    # KILL — lift 1.24
LEAD_STAGE2        = 0    # KILL — lift 1.00
LEAD_POCKET_PIVOT  = 0    # KILL — lift 0.99
LEAD_RS_NEW_HIGH   = 0    # KILL — lift 0.99
```

Demoted signals are **NOT deleted** — they remain as **informational overlay tells** on the PWA cards (so the user still sees "this fired a first-new-high"), but they contribute **zero to the score**. This preserves the OVERLAY-NOT-SCORER invariant.

Related framework finding (separate ADR-worthy point, recorded here for context): **momentum fails as a daily event-study signal** (lift 0.89 < 1) but **works as a quarterly portfolio factor** (12-1 momentum top-20: 36.5% CAGR TW / 32.3% US vs 23.7%/18.1% equal-weight). Conclusion: momentum is **not scored daily**; it lives as a separate「季度動能組合」lens. The SMA200 trend filter was *harmful* on the portfolio backtest (TW 36.5%→25.7%, US 32.3%→19.4%) and is left as historical reference, not run.

## Alternatives considered

1. **Keep the 82-name weights** — rejected: demonstrably overfit; would steer picks toward worse-than-random first-new-high names.
2. **Shrink weights instead of zeroing (e.g. halve all)** — rejected: a sub-1.0-lift signal has no edge to shrink toward; halving a negative-edge weight still injects noise into the score. Binary keep/kill on the CI gate is cleaner and defensible.
3. **Regime-conditional weights (keep 首次新高 only in FLAT regime where flat-lift was 2.95)** — rejected for now: the flat-regime lift is itself thin-sample on the small universe and there is no out-of-sample / deflated-Sharpe evidence it survives. Revisit only if a walk-forward + deflated-Sharpe re-run (see the validation.py upgrade) confirms a stable regime-conditional edge.

## Consequences / honest caveats

- The score now leans on just **2 leadership signals** — narrower but defensible. Picks will shift away from pure first-new-high breakouts.
- All backtests remain **survivorship-biased** (today's survivors); `BUSTED_PEERS` (17 boomed-then-busted names) is a *partial* counter, not a fix. Lift numbers are an **optimistic upper bound**.
- Monthly win rate of the portfolio sleeve is ~50% — the **edge is in amplitude, not frequency**.

## How to revisit safely

Re-run `python run_backtest.py` on the **full** universe and re-check the **CI>base + Bonferroni + BH** columns before changing any `LEAD_*`. Do **not** restore a weight on raw lift alone — that is exactly the trap that produced the 首次新高 = 15 mistake. The forthcoming `validation.py` (deflated-Sharpe + PBO + walk-forward folds + White/Hansen SPA) is the systemic guard against this class of error.
