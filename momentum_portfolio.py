# -*- coding: utf-8 -*-
"""動能組合（季度）lens — quarterly top-10 6-1 momentum PORTFOLIO view.

Decision: .decisions/2026-06-13-smartstock-15y-weight-gate.md.

Momentum is a PORTFOLIO-CONSTRUCTION factor (rank + hold + quarterly rebalance),
NOT a daily explosive signal: its event-study lift (0.89 < 1) vetoes it from the
live daily scorer (strategy.score_stock / rank_stocks). But the PORTFOLIO backtest
(backtest_portfolio.py, top-10 equal-weight quarterly rebalance, TW + US sleeves)
beats equal-weight + buy-hold (committed JSON: TW 48.3%/Sharpe 1.54, US 43.3%/1.29;
was written here as US 43.7% — the artifact says 0.4331, corrected 2026-08-03).
The SAME factor draws OPPOSITE conclusions in the two frameworks — this module
surfaces the PORTFOLIO conclusion as a SEPARATE lens.

⚠ BIAS (audit 2026-06-21; re-verified 2026-08-03): these CAGRs are a CURRENT-CONSTITUENT
UPPER BOUND. They carry BOTH survivorship bias (failed/delisted names never entered the
universe CSV) AND constituent look-ahead (2026 membership filters the 2012 cross-section).
The true point-in-time figure is materially lower, and nothing here has been checked with
DSR/PBO/SPA. Survivorship is not quantified in this repo (literature puts it around 2–4%/y).

BL-P2-2 status — WIRED BUT INERT, do not upgrade this claim without re-measuring:
backtest_portfolio.run_sleeve now takes `added_dates` and applies the same
run_backtest.apply_pit_membership the sibling harnesses use (CLI: on by default), so the
plumbing exists. It currently masks ZERO bars: build_added_dates.py derives `added_date`
from each name's FIRST CACHED BAR, and you cannot cut bars before the first bar — measured
2026-08-03, TW 150 dated names → 0 bars, US 502 → 0 bars, every metric bit-identical.
Real index-inclusion dates are not keyless-reconstructable, which is why the look-ahead
survives. run_optimize.run_grid is not wired at all (separate module/owner).
Each artifact records the MEASURED effect (`pit_membership.effective` / `bars_dropped` in
backtest_portfolio_*.json), surfaced per sleeve as `track_record.pit_enabled`; the
disclaimers below switch wording off that measurement — never off "masking was requested".

CONTRACT (golden-additive invariant):
  * PURE, injectable functions — ZERO network. Live universe histories are passed
    in by the caller (main.py threads the opportunity scan's OHLCV); the track
    record is READ from backtest_portfolio_*.json, NEVER recomputed here.
  * NOTHING here touches strategy.score_stock / rank_stocks. The lens is an
    informational sidecar in a separate payload key (`momentum_portfolio`).
  * momentum comes from factor_signals.mom_12_1 — imported, never modified. The LENS
    OVERRIDES the lookback to 126 (6-1), while factor_signals' default stays 252 (12-1)
    for the event-study harness.

Honest disclosure (decision §3 + §Momentum) is shipped ON-PAGE verbatim:
  - 季度再平衡策略，非當日進出
  - 月勝率 ~50%，edge 在幅度而非頻率（WilsonLo ≈ 0.51-0.54）
  - 以現行成分回測，報酬為樂觀上界（survivorship + 成分 look-ahead）
  - 與每日精選為不同框架
"""
import json
import logging

from factor_signals import mom_12_1

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 10        # #4 Calmar-winner: top-10 (was 20) — concentrated cohort
DEFAULT_LOOKBACK = 126    # #4 Calmar-winner: 6-1 momentum (was 12-1/252) — 6mo beat 12mo

# VERBATIM honest-disclosure lines (decision §3 揭露 + §Momentum 框架錯配). These
# are the canonical strings the PWA + report render; do NOT trim or paraphrase.
# DISCLAIMERS describes a track record built WITHOUT point-in-time membership (the
# pre-BL-P2-2 artifacts); PIT_DISCLAIMERS is the same list with the two bias lines
# restated for an artifact whose JSON reports pit_membership.enabled = true. Pick via
# disclaimers_for() — NEVER hard-code one list, the wording is a factual claim about
# how the committed numbers were produced.
DISCLAIMERS = [
    "季度再平衡策略，非當日進出 — 與每日精選為不同框架，請勿混用。",
    "月勝率約 50%（WilsonLo ≈ 0.51-0.54），edge 在好月份的『幅度』而非『頻率』，並非月月穩贏。",
    "報酬為樂觀『上界』：以『現在』的成分股回測整段 15 年（survivorship bias + 成分 look-ahead — "
    "2026 的成分名單拿去篩 2012 的橫斷面），真實 point-in-time 數字會明顯較低。",
    "與每日精選清單為不同框架（組合構建 vs 當日爆發訊號），informational，非買賣訊號。",
    "量化警語：current-constituent 上界；survivorship 未量化（估 2–4%/y 級）；未經 DSR/PBO/SPA 檢驗。",
]

# Same four themes, restated for a PIT-masked track record: the constituent-look-ahead
# clause would be FALSE there, the survivorship / multiple-testing clauses stay true.
PIT_DISCLAIMERS = [
    DISCLAIMERS[0],
    DISCLAIMERS[1],
    "報酬仍為樂觀『上界』：成分 look-ahead 已修（point-in-time 成分遮罩，加入指數前的 K 線不參與排名），"
    "但 universe 只含存活至今的名字 — survivorship bias 未修，真實數字仍偏低於此。",
    DISCLAIMERS[3],
    "量化警語：survivorship 未量化（估 2–4%/y 級，本專案未實測）；未經 DSR/PBO/SPA 多重檢定檢驗；"
    "成分 look-ahead 已由 point-in-time 遮罩移除。",
]


def disclaimers_for(pit_enabled):
    """Return the disclaimer list matching how the committed track record was built.

    pit_enabled : truthy only when EVERY surfaced sleeve reports pit_membership.enabled.
    Unknown / mixed / missing flag → the conservative (pre-PIT) wording, because an old
    artifact carries no flag and must NOT be described as point-in-time.
    """
    return list(PIT_DISCLAIMERS if pit_enabled else DISCLAIMERS)


def rank_momentum(histories, top_n=DEFAULT_TOP_N, names=None, lookback=DEFAULT_LOOKBACK):
    """Rank a {ticker: OHLCV-DataFrame} map by N-1 momentum, descending.

    Returns the top_n rows as dicts: {ticker, name, mom, price}. A name with
    insufficient bars / a bad frame (mom_12_1 → None) is EXCLUDED (never ranked
    on a fabricated value). Pure: input frames are never mutated.

    Parameters
    ----------
    histories : {ticker: DataFrame} | None  — live OHLCV (caller-injected; no net)
    top_n     : how many holdings to surface (default 20, the backtested cohort)
    names     : optional {ticker: display name}; missing → name == ticker
    """
    names = names or {}
    rows = []
    for ticker, df in (histories or {}).items():
        m = mom_12_1(df, lookback=lookback)   # None on short/None/bad frame → skip
        if m is None:
            continue
        price = None
        try:
            price = round(float(df["Close"].iloc[-1]), 2)
        except Exception:
            price = None
        rows.append({
            "ticker": ticker,
            "name": names.get(ticker) or ticker,
            "mom": m,
            "price": price,
        })
    rows.sort(key=lambda r: r["mom"], reverse=True)
    return rows[:top_n]


def read_track_record(path, strategy="momentum"):
    """Read a strategy segment (default `momentum`; pass `momentum_voltgt` for the P4
    constant-vol variant) of a backtest_portfolio_*.json file.

    Returns a flat dict of the metrics the lens displays, or None if the file is
    missing / corrupt / has no such segment (graceful — never raises). This NEVER
    recomputes the backtest; it only surfaces the committed numbers.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("SKIP track record %s: %s", path, e)
        return None
    strategies = (data or {}).get("strategies") or {}
    mom = strategies.get(strategy)
    if not mom:
        return None
    win = mom.get("monthly_win_vs_bench") or {}
    oos = mom.get("oos") or {}
    return {
        "cagr": mom.get("cagr"),
        "sharpe": mom.get("sharpe"),
        "max_dd": mom.get("max_dd"),
        "oos": {
            "cagr": oos.get("cagr"),
            "sharpe": oos.get("sharpe"),
            "max_dd": oos.get("max_dd"),
            "start": oos.get("start"),
            "end": oos.get("end"),
        },
        "monthly_win_rate": win.get("rate"),
        "monthly_win_lo": win.get("wilson_lo"),
        "equal_weight_cagr": (strategies.get("equal_weight") or {}).get("cagr"),
        "buy_hold_cagr": (strategies.get("buy_hold") or {}).get("cagr"),
        "n_universe": data.get("n_universe"),
        "period": data.get("period"),
        "top_n": data.get("top_n"),
        "start": data.get("start"),
        "end": data.get("end"),
        # BL-P2-2: did point-in-time masking actually REMOVE anything in this artifact?
        # Keyed off the measured `effective` flag, NEVER off "masking was applied" — a
        # first-bar-derived added_date column runs the mask and cuts nothing. Absent key
        # (pre-2026-08-03 artifact) → False; never assume PIT.
        "pit_enabled": bool(((data or {}).get("pit_membership") or {}).get("effective")),
    }


def build_lens(tw_histories, us_histories, backtest_tw_json, backtest_us_json,
               top_n=DEFAULT_TOP_N, tw_names=None, us_names=None,
               lookback=DEFAULT_LOOKBACK):
    """Assemble the full 動能組合 lens dict for the PWA payload / report.

    Returns:
      {
        "tw": {"holdings": [...], "track_record": {...}|None},
        "us": {"holdings": [...], "track_record": {...}|None},
        "disclaimers": [str, ...],
        "top_n": int,
      }

    Pure + injectable: histories are caller-supplied (no network); track records
    are read from the committed backtest JSON. Each sleeve degrades independently —
    an empty universe yields empty holdings, a missing JSON yields a None track
    record, neither raises. JSON-serializable throughout (flows into the payload).

    The disclaimer wording follows the ARTIFACTS: only when BOTH sleeves' JSON report
    pit_membership.enabled does the lens claim point-in-time membership (BL-P2-2).
    """
    tw_tr = read_track_record(backtest_tw_json)
    us_tr = read_track_record(backtest_us_json)
    pit = all((tr or {}).get("pit_enabled") for tr in (tw_tr, us_tr))
    return {
        "tw": {
            "holdings": rank_momentum(tw_histories, top_n=top_n, names=tw_names,
                                      lookback=lookback),
            "track_record": tw_tr,
            "track_record_voltgt": read_track_record(backtest_tw_json, "momentum_voltgt"),
        },
        "us": {
            "holdings": rank_momentum(us_histories, top_n=top_n, names=us_names,
                                      lookback=lookback),
            "track_record": us_tr,
            "track_record_voltgt": read_track_record(backtest_us_json, "momentum_voltgt"),
        },
        "disclaimers": disclaimers_for(pit),
        "pit_membership": bool(pit),
        "voltgt_note": ("vol-target σ%.2f：縮放同批持股至定波動，回撤大降（OOS -39%%→約-22%%）"
                        "但 CAGR 較低——低回撤版，非取代。" % 0.15),
        "top_n": top_n,
    }
