# Decision: demote the institutional-flow base factors (`inst_foreign_buy`/`inst_foreign_sell`/`inst_trust_buy`) 15/-20/10 → 0

**Date:** 2026-08-03
**Status:** APPLIED (user-approved under standing demotion-only governance — near_high precedent)
**Scope:** changes `strategy.score_stock` → changes every daily pick with institutional (法人) input (live scorer change)

## Evidence

`factor_panels_aux.instflow_panel` — a keyless, PIT-lagged, multi-year institutional-net-flow
panel built from the TWSE T86 OpenAPI (`(foreign+trust) net shares / 20d traded volume`) — was
run through the rigorous aux-combo IC-screen pipeline and **FAILED**:

- `optimize_tw_aux.txt` (commit `01025ee4f`, 2026-07-14): `IC screen (search-span, floor 0.05,
  next-bar fill): {"instflow": -0.012249089911153829, "value": 0.06129370547930897}`,
  `survivors: ['value'] | n_trials=3`.
- `instflow` was **screened and failed on the merits** (rank-IC -0.0122, below the 0.05 floor) —
  it is not in the pipeline's separate "excluded (no keyless multi-year history)" bucket
  (that bucket is margincontra/revmom/quality/size).
- Meanwhile `config.py` (pre-fix) unconditionally awarded `inst_foreign_buy=+15`,
  `inst_foreign_sell=-20`, `inst_trust_buy=+10` — this negative result was never reconciled
  with the live scorer, and `check_factor_drift.py`'s watch-list had no entry that could ever
  surface the contradiction (audit findings B5-06 / B5-05, `smartstock-audit/2026-08-03-evidence`).
- STRATEGY.md:11's own disclosure claims institutional-flow factors are untested "because keyless
  歷史籌碼/法人資料不可得" — `factor_panels_aux.py` directly contradicts that premise for
  `inst_foreign`/`inst_trust` specifically (a keyless multi-year T86-based panel DOES exist and
  WAS tested; the "no keyless history" framing correctly applies only to `chip_conc`'s
  broker-branch-level data, per `chip_state.py`).

## Caveat (documented, not hidden)

The aux combo's `instflow_panel` is a **combined** foreign+trust net/volume ratio
(`factor_panels_aux.py`). The live scorer's institutional factors use a **liquidity-gated
buy/sell/trust split** (`strategy.py:117-128`, `INST_RATIO_FULL`/`INST_RATIO_HALF` thresholds) —
same underlying institutional-flow hypothesis, tested via a **sibling implementation**, not
literally the same code path. This weakens (but does not remove) the inferential link between
the aux-combo's negative IC and the live scorer's specific formulation. Demoted anyway under
the repo's existing demotion-only governance (below), with the caveat stated explicitly here
rather than silently assumed away.

## Considered alternatives

1. **Leave the +15/-20/+10 weights untouched, note the caveat only in STRATEGY.md.** REJECTED —
   this is exactly the "evidence found and not propagated" failure mode the audit flagged (B5-06):
   a real negative result sitting unused while the live scorer keeps awarding points on the same
   hypothesis. Documentation without action is not a remediation.
2. **Re-implement the live scorer's inst_* factors to exactly match the aux combo's combined
   ratio, then re-run the IC screen on that exact formulation before deciding.** REJECTED for this
   pass — correct in principle (would remove the caveat entirely) but is new engineering work
   beyond a demotion-only governance action, and the repo's own precedent (vol_stable, near_high)
   demotes on same-hypothesis evidence from a sibling test rather than requiring byte-identical
   code paths. Re-implementing without first demoting the currently-known-weak weights would
   leave three unreconciled negative-evidence points live even longer.
3. **Demote `inst_foreign_buy`/`inst_foreign_sell`/`inst_trust_buy` to 0 now; re-promote only
   after a keyless IC re-gate of the split formulation.** CHOSEN — matches the repo's existing
   demotion-only governance (`vol_stable` 10→0, `near_high` 20→0, 5 leadership signals→0): weight
   goes to 0, the factor stays registered in `FACTOR_PTS` (reversible, no `strategy.py` edit,
   0-weight factors never enter the `factors` dict so chip cards drop them naturally). The caveat
   above is recorded so a future re-promotion decision starts from an accurate picture rather than
   assuming the sibling-implementation evidence was as strong as a same-code-path re-test.

## Effect

`config.FACTOR_PTS["inst_foreign_buy"] = 0`, `["inst_foreign_sell"] = 0`, `["inst_trust_buy"] = 0`.
`strategy.py:123-128` already guards on each weight (`if ... and FACTOR_PTS["inst_foreign_buy"]:`
etc.), so 外資買超/外資賣超/投信買超 simply stop contributing — verified via
`test_smartstock.py`'s updated institutional tests. `check_factor_drift.py` gets a new
`instflow` watch-list entry (mapped to these three keys) so a future keyless IC re-gate result
for this family has a feedback channel into the monthly HITL drift monitor, per BL-P1-12(b).

`sector`/`chip_conc_*`/`streak` remain outside any backtest coverage (B5-05) — STRATEGY.md
already self-discloses this; no change needed there per BL-P1-12(c).

## Reversal

Set the three weights back to 15/-20/10. No structural change; a one-line-per-key revert once a
passing keyless IC re-gate of the live scorer's actual split formulation exists.
