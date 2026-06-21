# -*- coding: utf-8 -*-
"""動能組合（季度）lens — quarterly top-10 6-1 momentum PORTFOLIO view.

Decision: .decisions/2026-06-13-smartstock-15y-weight-gate.md.

Momentum is a PORTFOLIO-CONSTRUCTION factor (rank + hold + quarterly rebalance),
NOT a daily explosive signal: its event-study lift (0.89 < 1) vetoes it from the
live daily scorer (strategy.score_stock / rank_stocks). But the PORTFOLIO backtest
(backtest_portfolio.py, top-10 equal-weight quarterly rebalance, TW + US sleeves)
beats equal-weight + buy-hold (committed JSON: TW 48.3%/Sharpe 1.54, US 43.7%/1.29).
The SAME factor draws OPPOSITE conclusions in the two frameworks — this module
surfaces the PORTFOLIO conclusion as a SEPARATE lens.

⚠ BIAS (audit 2026-06-21): these CAGRs are a CURRENT-CONSTITUENT UPPER BOUND, not
point-in-time. The backtest uses TODAY's index members across the whole 15y history
(added_date is not yet masked), so it carries BOTH survivorship bias (failed/delisted
names never entered) AND constituent look-ahead (2026 membership filters the 2012
cross-section). The true PIT figure is materially lower. Wiring added_date masking
into run_sleeve/run_grid is the pending fix.

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
DISCLAIMERS = [
    "季度再平衡策略，非當日進出 — 與每日精選為不同框架，請勿混用。",
    "月勝率約 50%（WilsonLo ≈ 0.51-0.54），edge 在好月份的『幅度』而非『頻率』，並非月月穩贏。",
    "報酬為樂觀『上界』：以『現在』的成分股回測整段 15 年（survivorship bias + 成分 look-ahead — "
    "2026 的成分名單拿去篩 2012 的橫斷面），真實 point-in-time 數字會明顯較低。",
    "與每日精選清單為不同框架（組合構建 vs 當日爆發訊號），informational，非買賣訊號。",
]


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
    """
    return {
        "tw": {
            "holdings": rank_momentum(tw_histories, top_n=top_n, names=tw_names,
                                      lookback=lookback),
            "track_record": read_track_record(backtest_tw_json),
            "track_record_voltgt": read_track_record(backtest_tw_json, "momentum_voltgt"),
        },
        "us": {
            "holdings": rank_momentum(us_histories, top_n=top_n, names=us_names,
                                      lookback=lookback),
            "track_record": read_track_record(backtest_us_json),
            "track_record_voltgt": read_track_record(backtest_us_json, "momentum_voltgt"),
        },
        "disclaimers": list(DISCLAIMERS),
        "voltgt_note": ("vol-target σ%.2f：縮放同批持股至定波動，回撤大降（OOS -39%%→約-22%%）"
                        "但 CAGR 較低——低回撤版，非取代。" % 0.15),
        "top_n": top_n,
    }
