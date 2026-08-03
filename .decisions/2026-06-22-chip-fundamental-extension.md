# Decision: extend the rigorous factor combo with keyless chip/fundamental panels (iteration-2 aux)

**Date:** 2026-06-22
**Status:** APPLIED
**Scope:** adds `factor_panels_aux.py` / `build_aux_panels.py` / `run_aux_combo.py` +
`optimize-aux.yml` (monthly CI); extends `run_optimize.factor_panel` / `sleeve_daily_rets` /
`rigorous_combo` with an optional `aux` panel set + `n_trials` deflation. Does not change the
price-only iteration-1 combo (byte-identical when `aux=None`).

## Note on provenance (added 2026-08-03, R7-07 audit remediation)

This file did not exist until now. `optimize-aux.yml`, `factor_panels_aux.py`,
`run_aux_combo.py`, and `run_optimize.py` all cite `.decisions/2026-06-22-chip-fundamental-
extension` as the ADR backing this feature (5 references — audit finding R7-07, confirmed the
file was genuinely missing, not just misnamed: none of the 8 `.decisions/` files present at
audit time matched). The decision itself was real and was recorded in the two commit messages
that shipped it (`c4db1e1ad`, `f02c3ab83`, both 2026-06-22) — it was simply never promoted from
commit message to a `.decisions/` file per Rule 26's Decision Journal convention. This file
reconstructs that record from those two commits (quoted below) to close the dangling reference
at its target rather than editing 5 call sites across 4 files this remediation batch does not
own. No new information is invented beyond what the original commits already state.

## Context

Iteration-1 (`PREREG_CONFIGS`: lowvol/strev/mom) is a price-only pre-registered combo. This
extension adds a SECOND, separately-gated pre-registered combo built from KEYLESS TWSE/MOPS
chip and fundamental data (T86 institutional flow, monthly-revenue YoY, BWIBBU value ratios,
MI_MARGN margin balance, financial-statement quality/size proxies) — explicitly NOT FinMind or
other paid/third-party sources, matching the repo's keyless-only constraint.

## Considered alternatives

1. **Include broker-branch (券商分點) concentration and multi-year chip-concentration history
   as additional aux families.** REJECTED — "no keyless multi-year history exists" for either
   (3-round research including community sources confirmed paid/no-archive only); excluded from
   `AUX_FACTOR_FAMILIES` from the start rather than built and later dropped.
2. **Treat the aux combo's DSR as if it were a fresh, independent search (`n_trials=1`).**
   REJECTED — the aux extension is a SECOND pre-registered trial layered on top of the already-
   spent iteration-1 trial; understating `n_trials` would overstate the combo's deflated Sharpe.
   `rigorous_combo(..., n_trials=2)` was used at ship time (iteration-1 price combo=1 + this
   extension=1, cumulative). (A later hardening pass, `f02c3ab83`, refined this further to
   `n_trials = 1 + #aux candidates actually IC-screened`, since keep-iff-IC is itself a form of
   data-dependent selection — a stricter, not looser, correction.)
3. **Ship all 6 built aux panels (instflow/revmom/value/margincontra/quality/size) into the
   gated combo unconditionally.** CHOSEN at first cut, then narrowed same-day: the adversarial
   review in `f02c3ab83` found `margincontra` has no keyless multi-year 流通股 (float shares)
   series, so it would score on a volume PROXY rather than the actual ratio it claims to measure
   — a different, unvalidatable factor that could falsely PASS the gate. `margincontra` was
   dropped before the combo ran; the surviving candidate set is IC-screened (Phase-0, floor
   `IC_MIN`) before the final gated run, so even the panels that DO get built are not all
   guaranteed to reach the combo.

## Effect

- `factor_panels_aux.py`: 6 KEYLESS panels with point-in-time availability lag baked in
  (`lag_panel`, `PIT_LAG` per family) — the single guard against look-ahead bias.
- `run_optimize.py`: `AUX_FACTOR_FAMILIES` / `AUX_PREREG_CONFIGS` kept SEPARATE from
  `PREREG_CONFIGS` so the price-only combo stays valid and byte-identical when aux is absent;
  `rigorous_combo(..., aux=..., n_trials=...)` threads the extension through without touching
  the iteration-1 call sites.
- `build_aux_panels.py` / `run_aux_combo.py`: keyless date-by-date backfill (T86/BWIBBU/
  MI_MARGN, field indices live-verified) + incremental parquet cache + the Phase-0 rank-IC
  screen → pre-registered gate driver; `optimize-aux.yml` runs it monthly in CI.
- Hardening (`f02c3ab83`, same day, post adversarial review): calendar-align every aux raw
  panel to the price grid with bounded ffill before rolling/division (kills cross-calendar
  union-NaN injection); `panel_rank_ic` uses next-bar fill (no value-lag-0 IC leak); IC screen
  runs on the search span only, excluding the terminal lockbox (no selection leak); `min_names
  >= 2` floor on aux rebalance dates (no degenerate single-name baskets).

Downstream: a later decision (`2026-08-03-instflow-demotion.md`) found the `instflow` aux panel
specifically failed this screen on the merits (rank-IC below the floor) and used that result to
demote the LIVE scorer's related institutional-flow base factors — see that file for the
caveat on how directly the two implementations correspond.

## Reversal

Remove the `aux=` wiring from `rigorous_combo`/`sleeve_daily_rets` call sites (falls back to the
price-only iteration-1 combo); stop running `optimize-aux.yml`. No structural change to
iteration-1.
