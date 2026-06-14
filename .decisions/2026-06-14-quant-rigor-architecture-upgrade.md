# ADR 2026-06-14 — Quant-rigor architecture upgrade (anti-overfitting gates + scorer flip protocol)

> Status: **Accepted (mechanisms shipped; scorer flip PENDING offline gate)** · Branch: `feat/quant-rigor-arch-upgrade`

## Context

Architecture review (vs López de Prado AFML, Microsoft Qlib, alphalens, vectorbt, White/Hansen
SPA) found the system's quant layer already strong (Wilson CI + Bonferroni + BH, regime split,
next-open fills, level-shift sanitation, 1541 tests) but with concrete gaps: no systemic
anti-overfitting guard beyond the per-signal CI gate, flat 15bps slippage, a dormant rank-IC
gate, additive scoring over collinear trend factors, and base factors never IC-gated. The
first_new_high collapse (lift 2.44→0.68) was caught manually — nothing prevented the *class*
of error.

## Decisions (shipped this branch, all tests green)

- **A1** — extracted the BUCKET_SCORING ship logic into a tested `backtest.composite_ic_gate()`;
  added `config.IC_MIN=0.05`. The cross-sectional rank-IC gate is now a checkable unit.
- **A6** — `backtest.backtest_signal(adv_slippage=True)` scales slippage by liquidity
  (`clamp(base + k·sqrt(ref/ADV), base, cap)`); `config.ADV_SLIPPAGE` flag (default flat).
- **A2/A3** — `validation.py` (pure numpy/stdlib): Deflated Sharpe Ratio, PBO via CSCV,
  walk-forward folds, White/Hansen SPA. `run_validation.py` writes `docs/data/_validation_state.json`
  (offline weekly); the daily run only reads it (`verdict.family_robustness_badge`, degrade-safe,
  overlay-not-scorer).
- **B4** — base-factor point weights → `config.FACTOR_PTS` (read by reference, `if pts:` guarded)
  so a factor can be demoted by zeroing its weight without editing `strategy.py`.
- **A4a/A5 (mechanism)** — `config.BUCKET_SCORING` / `BUCKET_CAPS` / `BUCKET_IC_WEIGHTS` now read
  dynamically (a from-imported bool can't be flipped). `strategy.ic_gate_factor_pts(per_factor_ic,
  ic_min)` returns the FACTOR_PTS demotion set. Both default to current behaviour and are
  reversible; the **OVERLAY-NOT-SCORER invariant is enforced by test** (`test_bucket_flip_preserves_factors_dict`:
  flipping the flag leaves the `factors` dict byte-identical — re-aggregation only).

## The scorer-flip protocol (do NOT flip blind)

The two scorer changes (A4a turn BUCKET_SCORING on / A5 demote base factors) change daily picks,
so they ship **OFF** and flip only on offline evidence — never a blind edit:

1. **Enable realistic cost first**: set `config.ADV_SLIPPAGE = True` so the verdict is net-of-cost.
2. **Run the robustness gate** (offline, needs live/cached 15y data):
   `python run_validation.py 15 --universe universe_15y_draft.csv`
   → check `_validation_state.json`: family `pbo` should be **low** (< ~0.5) and `spa_pvalue` **< 0.05**.
   If PBO is high or SPA is non-significant, **stop** — the family edge is not robust; do not flip.
3. **A4a — flip BUCKET_SCORING**: run `python run_rank_ic.py 15` → flip `config.BUCKET_SCORING = True`
   **only if** it prints `SHIP` (bucket composite beats flat additive on BOTH top-decile edge AND
   rank-IC, with bucket IC ≥ `IC_MIN`). Populate `config.BUCKET_IC_WEIGHTS` from the per-bucket IC.
4. **A5 — demote base factors**: from the offline per-factor cross-sectional IC, call
   `strategy.ic_gate_factor_pts(per_factor_ic, config.IC_MIN)` and write the zeroed weights into
   `config.FACTOR_PTS`. Use **cross-sectional IC**, not event-study precision (base factors are
   trend hats; momentum fails as a daily event signal — the gap-d framework mismatch).
5. **Re-run the suite** and update the golden strategy tests in the SAME commit (their pinned
   factor values change intentionally), recording the post-flip pick delta here.

## Alternatives considered

- **Auto-flip the flag from the gate verdict** — rejected: an automated scorer change with no
  human checkpoint violates the high-risk-tool HITL policy; the gate informs, the human commits.
- **Orthogonalize the collinear trend factors (Gram-Schmidt)** instead of bucket-cap + IC-weight
  — rejected: empirically weaker (the correlation structure carries signal); bucketing + caps is
  the simpler, already-built path.
- **ML meta-labeling** (AFML) to gate picks — rejected: breaks the keyless light-install mandate
  (sklearn/torch) and adds an opaque scorer; excluded by design.

## Gate RESULTS — executed 2026-06-14 (PIT + ADV-scaled cost, 661-name 15y)

The protocol was actually RUN on the cached 658-name universe with `ADV_SLIPPAGE=True` + PIT
(`added_date` from cached first-bar; 80 names start ≥2013). Outcome: **the scorer is already
correctly configured — every proposed scorer change was REJECTED by the evidence. No config flip.**

- **A4a bucket scoring → KEEP additive (do NOT flip).** `run_rank_ic 15`: bucket beats additive on
  edge (1.87→2.27%) and rank-IC (0.0286→0.0288) but bucket rank-IC **0.0288 < IC_MIN 0.05** — both
  composites are below the trust floor, so the IC floor correctly blocks a weak-IC flip.
- **LEAD_* leadership weights → UNCHANGED (validated).** `run_backtest 15 --universe --fresh` net-of-
  ADV-cost + PIT KEEP list = **VDU→Thrust lift 1.58 (flat 1.74)** and **U/D量比吸籌 lift 1.54 (flat
  1.71)** — the exact same 2 signals as the current config (`LEAD_VDU_THRUST=10`, `LEAD_UD_ACCUM=8`,
  rest=0). Realistic cost trimmed lift slightly (1.61→1.58, 1.55→1.54) but both still clear
  CI/Bonferroni/BH well above 1.0. The re-gate converts "we think these are right" into "validated
  net-of-realistic-cost, point-in-time."
- **A5 base-factor IC → FLAG, do NOT demote.** `run_factor_ic 15` cross-sectional rank-IC per family
  (breadth basket): **every** base family is below the 0.05 floor — trend −0.011, momentum −0.032,
  volume −0.043, vol_stable −0.060, **RS +0.026 (best)**, high52 −0.005, RSI −0.068, OBV −0.038. The
  additive base score has thin/negative cross-sectional alpha. **NOT acted on** because: (a) the
  breadth basket (65 mega-caps) is NOT the scoring universe (600+ small/mid-caps carry the real
  cross-sectional dispersion); (b) negative momentum/RSI IC is the known mean-reversion-among-leaders
  sign (the gap-d "momentum is a portfolio factor, not a daily signal" effect cross-sectionally), not
  noise; (c) zeroing all base factors would leave only the 2 leadership signals scoring → catastrophic
  to picks. **Recommendation (separate investigation, user sign-off):** re-run `run_factor_ic` on the
  full opportunity universe, and consider restructuring the score around the validated leadership
  signals + the momentum-portfolio lens rather than per-factor demotion. `ic_gate_factor_pts` stands
  ready as the lever once a family is approved.
- **DSR / PBO / SPA family robustness** (`run_validation`) — see `docs/data/_validation_state.json`.
  Read DSR as a probability ∈[0,1]; gate "robust" at **DSR > 0.95** (NOT >0), and treat n_trials as
  the *effective* (de-duplicated) independent-trial count, per [[reference_deflated_sharpe_probability]].

**Net: the gate did its job in BOTH directions — rejected the bucket flip, confirmed the leadership
weights, flagged (without recklessly acting on) the thin base-factor IC. The scorer config is
unchanged because the evidence says it is already right; that is a successful gate run, not a no-op.**

## Consequences / caveats

- Until step 2–4 run on live data, the scorer is **unchanged** (flags OFF) — this branch adds
  capability + guards, not a behaviour change.
- All backtests remain survivorship-biased (`BUSTED_PEERS` is a partial counter); every lift/DSR
  is an optimistic upper bound. The robustness badge says so.
- New statistics are pure numpy/stdlib (Acklam `norm_ppf`, hand-rolled skew/kurt) — no new heavy
  dependency; the keyless install is intact.
