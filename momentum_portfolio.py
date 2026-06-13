# -*- coding: utf-8 -*-
"""動能組合（季度）lens — quarterly top-20 12-1 momentum PORTFOLIO view.

Decision: .decisions/2026-06-13-smartstock-15y-weight-gate.md.

Momentum is a PORTFOLIO-CONSTRUCTION factor (rank + hold + quarterly rebalance),
NOT a daily explosive signal: its event-study lift (0.89 < 1) vetoes it from the
live daily scorer (strategy.score_stock / rank_stocks). But the PORTFOLIO backtest
(backtest_portfolio.py, top-20 equal-weight quarterly rebalance, TW + US sleeves,
OOS last 2y) proved it beats equal-weight + buy-hold (TW 36.5%/Sharpe 1.42,
US 32.3%/Sharpe 1.13). The SAME factor draws OPPOSITE conclusions in the two
frameworks — this module surfaces the PORTFOLIO conclusion as a SEPARATE lens.

CONTRACT (golden-additive invariant):
  * PURE, injectable functions — ZERO network. Live universe histories are passed
    in by the caller (main.py threads the opportunity scan's OHLCV); the track
    record is READ from backtest_portfolio_*.json, NEVER recomputed here.
  * NOTHING here touches strategy.score_stock / rank_stocks. The lens is an
    informational sidecar in a separate payload key (`momentum_portfolio`).
  * 12-1 momentum comes from factor_signals.mom_12_1 (LOOKBACK=252 / SKIP=21) —
    imported, never modified.

Honest disclosure (decision §3 + §Momentum) is shipped ON-PAGE verbatim:
  - 季度再平衡策略，非當日進出
  - 月勝率 ~50%，edge 在幅度而非頻率（WilsonLo ≈ 0.50）
  - 以現行成分回測，報酬為樂觀上界（survivorship）
  - 與每日精選為不同框架
"""
import json
import logging

from factor_signals import mom_12_1

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 20

# VERBATIM honest-disclosure lines (decision §3 揭露 + §Momentum 框架錯配). These
# are the canonical strings the PWA + report render; do NOT trim or paraphrase.
DISCLAIMERS = [
    "季度再平衡策略，非當日進出 — 與每日精選為不同框架，請勿混用。",
    "月勝率約 50%（WilsonLo ≈ 0.50），edge 在好月份的『幅度』而非『頻率』，並非月月穩贏。",
    "以現行成分股回測，存在 survivorship bias，報酬為樂觀『上界』。",
    "與每日精選清單為不同框架（組合構建 vs 當日爆發訊號），informational，非買賣訊號。",
]


def rank_momentum(histories, top_n=DEFAULT_TOP_N, names=None):
    """Rank a {ticker: OHLCV-DataFrame} map by 12-1 momentum, descending.

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
        m = mom_12_1(df)              # None on short/None/bad frame → skip
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


def read_track_record(path):
    """Read the `momentum` strategy segment of a backtest_portfolio_*.json file.

    Returns a flat dict of the metrics the lens displays, or None if the file is
    missing / corrupt / has no momentum segment (graceful — never raises). This
    NEVER recomputes the backtest; it only surfaces the committed numbers.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("SKIP track record %s: %s", path, e)
        return None
    strategies = (data or {}).get("strategies") or {}
    mom = strategies.get("momentum")
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
               top_n=DEFAULT_TOP_N, tw_names=None, us_names=None):
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
            "holdings": rank_momentum(tw_histories, top_n=top_n, names=tw_names),
            "track_record": read_track_record(backtest_tw_json),
        },
        "us": {
            "holdings": rank_momentum(us_histories, top_n=top_n, names=us_names),
            "track_record": read_track_record(backtest_us_json),
        },
        "disclaimers": list(DISCLAIMERS),
        "top_n": top_n,
    }
