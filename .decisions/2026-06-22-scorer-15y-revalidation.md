# Decision: 15y scorer re-validation + event-TA signal discovery — no new demotion, no new signal

**Date:** 2026-06-22
**Status:** APPLIED (user-approved plan: "15y 驗證版每日分析 + 風控組合 track，全程誠實標註")
**Scope:** Phase 0 (event-TA discovery) + Phase 1 (scorer re-validation) — both conclude NO live
scorer change (zero golden churn). Phase 2/3 add an INFORMATIONAL track (no scorer change).

Prompted by the user's challenge: "應該不可能找不到贏大盤的策略…台灣主動基金報酬遠超大盤…試過布林
通道等技術指標嗎". Answered with evidence below (the fund premise is largely survivorship +
structural-edge selection; see §Context). Honest framing per [[2026-06-21-vol-stable-demotion]].

## Phase 0 — event-type classic-TA signals: ALL FAIL the lift gate
Cross-sectional TA was already dead this session (Bollinger %b / band-width / MACD / KD / Williams
%R / CCI rank-IC all |IC| ≤ 0.0146 < 0.05 floor — deterministic transforms of the same price
series hold no ranking edge). The UNTESTED mode = EVENT-study (the mode VDU→Thrust 1.61 / U/D accum
1.55 passed). Pre-registered 4 event predicates (`ta_event_signals.py`, 10 TDD tests), ran the SAME
Wilson-CI lift gate (`run_ta_event_backtest.py`, horizon 60d, explosive ≥+25%, fee 30bps, next-open,
FIRED_FLOOR 100, m=4 correction). Gate self-validated: benchmark **U/D accum reproduced lift 1.35,
KEEP=YES**.

| signal | fired | lift | flat-lift | CI>base | Bonferroni | verdict |
|---|---|---|---|---|---|---|
| Bollinger squeeze→breakout | 368 | 1.03 | 0.68 | n | n | FAIL (no edge, flat=beta) |
| Donchian 20d breakout | 2042 | 0.89 | 0.76 | n | n | FAIL (below base) |
| **KD golden-cross (oversold)** | 1214 | **1.21** | **1.22** | **Y** | **n** | borderline — beats base + BH + not-beta, **fails Bonferroni** (p=0.0134 > 0.0125) |
| MACD zero-axis cross-up | 409 | 0.84 | 0.94 | n | n | FAIL |

**Decision: add NOTHING.** KD-golden-cross-from-oversold is the only mechanistically-motivated near-
miss; per anti-p-hacking discipline it does NOT clear the family-wise gate. A future iteration MAY
pre-register KD ALONE (m=1, cumulative n_trials counted) — that is the only ADR-compliant path; do
NOT re-mine. `signal_registry.LEADERSHIP` + `config.LEAD_*` UNCHANGED. (Answers "試過布林嗎": yes —
Bollinger event mode lift 1.03/flat 0.68, no edge.)

## Phase 1 — scorer 15y re-validation: NO new demotion warranted
Re-read the canonical full-universe (644-name) 15y rank-IC (`docs/data/_factor_ic_state.json`).
Applying the SAME two-metric rule from the vol-stable ADR (demote only factors weak on BOTH
rank-IC AND top-decile edge):

| family | rank_ic | edge | call |
|---|---|---|---|
| rs +0.0306 / mom +0.0166 / trend +0.0112 / high52 +0.0065 / volume +0.0043 / obv +0.0019 | <0.05 but **positive** | edge 1.34–4.18 (all positive) | KEEP (weak-IC but real positive edge) |
| rsi −0.0115 | neg (non-monotonic, expected) | 2.31 | KEEP (edge healthy) |
| vol_stable −0.025 | neg | 0.56 | already demoted 2026-06-21 |

**Decision: no further demotion** → `config.FACTOR_PTS` UNCHANGED → **zero golden churn**. Every
reward factor retains a positive top-decile edge; mechanically applying IC_MIN=0.05 would gut the
scorer (rejected, same reasoning as the precedent). Honest reframe surfaced to the user: the live
11-factor scorer was NEVER SPA-tested vs the index and its factors are weak (IC<0.05) — it is a
15y-validated **watchlist/assist**, NOT a proven index-beater. The PWA banner (Phase 3) states this.

Per-market (TW-only / US-only) IC split: deferred to Phase 4 CI as supplementary evidence. The mixed
IC is conservative for the intersection rule (positive in the mix ⇒ not dead in BOTH markets), so it
cannot mask a both-markets-dead factor — the "no demotion" call is safe on existing evidence.

## Phase 2/3 — what WAS added (informational only, no scorer change)
`validated_portfolio.py` surfaces the pre-registered LOWVOL+STREV+MOM combo
(`optimize_tw.json["combo"]`: DSR 0.998✓ / PBO 0.293✓ / lockbox CAGR 10.7% / MaxDD −21%✓ but SPA
p=0.17✗ / flat-lift 0.81✗ → **overall not-pass**) as a daily track ALONGSIDE the passive 0050/SPY
benchmark, with an always-on "不承諾贏過大盤指數" banner. E2E check: passive 0050 15y CAGR **11.4%**
(MaxDD −32%) actually OUT-RETURNS the active combo's lockbox 10.7% — the combo's only value is lower
drawdown. Additive payload key, golden byte-identity intact (14/14). Does NOT and is NOT claimed to
beat the index.

## Context — "active funds beat the index, so alpha must exist"
Largely survivorship + selective attention: SPIVA shows most TW active funds UNDERPERFORM the
benchmark after fees over 10–15y; winners don't persist beyond chance. The minority that genuinely
beat use edges unavailable keyless (analyst/information edge, paid private data, IPO allocations,
leverage+shorting, institutional low cost). Real factor premia DO exist (mom/value IC 0.01–0.05 —
found here) but below the strict bar; a pro harvests them with leverage+low-cost+diversification,
NOT statistical-significance-vs-index. We DID find a real deployable risk-managed combo; "no index
beat" means no SPA SIGNIFICANCE — a bar even good funds rarely clear in a test.

## Reversal
Nothing to revert in the live scorer (no change made). The informational track is removed by
deleting the `validated_portfolio` payload key + the `validatedHtml` tab (one line each).
