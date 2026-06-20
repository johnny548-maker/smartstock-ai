# Decision: demote the `vol_stable` base scoring factor (10 → 0)

**Date:** 2026-06-21
**Status:** APPLIED (user-approved — "因子 demote 依照你的建議")
**Scope:** changes `strategy.score_stock` → changes every daily pick (live scorer change)

## Evidence
Full-universe (644 names) 15y cross-sectional rank-IC per base-factor family
(`docs/data/_factor_ic_state.json`, run_factor_ic.py):

| family | rank_ic | top-decile edge |
|---|---|---|
| rs | +0.0306 | 4.18 |
| momentum | +0.0166 | 1.87 |
| trend | +0.0112 | 2.62 |
| high52 | +0.0065 | 1.34 |
| volume | +0.0043 | 1.95 |
| obv | +0.0019 | 2.29 |
| **rsi** | **-0.0115** | 2.31 |
| **vol_stable** | **-0.025** | **0.56** |

## Considered alternatives
1. **Apply the IC_MIN=0.05 floor mechanically (demote all 8).** REJECTED — IC_MIN=0.05
   is miscalibrated for SINGLE base factors (a normal single-factor cross-sectional IC is
   ~0.01–0.05). Every family is below it; blindly gating would zero the entire scorer. The
   floor is a family-screen threshold, not a single-factor one.
2. **Demote both negative-IC factors (vol_stable AND rsi).** REJECTED for rsi — RSI is
   NON-MONOTONIC (overbought penalty + oversold reward), so a negative Spearman rank-IC is
   *expected* and not evidence of no edge; rsi's top-decile edge (2.31) is healthy. Demoting
   it on a metric that mismatches its shape would be wrong.
3. **Demote `vol_stable` only.** CHOSEN — it is the ONLY family weak on BOTH metrics:
   negative rank-IC (-0.025) AND the weakest top-decile edge by far (0.56, less than half of
   every other family). It is a risk-character reward (low-volatility names), not an alpha
   factor; removing it also tilts the scorer toward higher return, matching the user's stated
   "預設要高報酬" preference.

## Effect
`config.FACTOR_PTS["vol_stable"] = 0`. strategy.py:107 already guards on the weight, so the
"波動穩定" factor simply stops contributing (verified: a low-vol name no longer emits it).
Golden-additive invariant intact (both overlay/no-overlay sides change equally → still
byte-identical). Full suite 1644 passed. The other 7 families KEPT.

## Reversal
Set the weight back to 10. No structural change; one-line revert.
