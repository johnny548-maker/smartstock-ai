# -*- coding: utf-8 -*-
"""驗證組合 track — the 15y-validated, risk-MANAGED combo surfaced as a daily lens.

Decision: .decisions/2026-06-21-factor-family-preregistration.md + this iteration's plan. The
pre-registered LOWVOL+STREV+MOM inverse-vol combo (run_optimize.rigorous_combo) is the best
DEPLOYABLE result of the whole rigorous search: it PASSES DSR (0.998) / PBO (0.293) / lockbox
(TW CAGR ~10.7%, MaxDD ~-21%) — a real, holdable, risk-managed sleeve — but it FAILS the
beats-the-index tests (SPA p=0.17, flat-regime lift 0.81<1 = beta not alpha), so overall_pass=False.

This module surfaces that combo as an INFORMATIONAL daily track alongside the PASSIVE benchmark
(0050 / SPY), exactly mirroring momentum_portfolio.py:
  * PURE, injectable — ZERO network. Live histories are caller-supplied (main.py threads the
    opportunity scan's OHLCV); the track record is READ from optimize_<sleeve>.json["combo"],
    NEVER recomputed.
  * NOTHING here touches strategy.score_stock / rank_stocks (golden-additive invariant).
  * Honest framing is shipped verbatim: it does NOT and is NOT claimed to beat the index;
    the full gate verdict (including every FAIL) is carried through, never hidden.

GOTCHA — holdings vs track record: compute_daily_targets applies the combo's factor RECIPE to
TODAY's fetched universe → INDICATIVE holdings, NOT a universe-pinned reproduction of the gated
backtest. The CAGR/MaxDD in the track record ARE universe-pinned. The disclaimers keep them apart.
"""
import json
import logging

import backtest_portfolio as bp
import run_optimize as ro

log = logging.getLogger(__name__)

# VERBATIM honest-disclosure lines — the canonical strings the PWA + report render. Do NOT trim.
DISCLAIMERS = [
    "經 15 年驗證、可重現的風險管理組合（LOWVOL+STREV+MOM 反向波動加權）；通過 DSR/PBO/lockbox，"
    "TW lockbox CAGR ~10.7% / 最大回撤 ~-21%。",
    "但統計上『未顯著贏過大盤』（SPA p=0.17、平盤 lift 0.81<1 = 偏 beta 非 alpha），overall=未過。"
    "本組合協助決策，不承諾贏過指數 — 被動持有 0050/SPY 仍是合理基準。",
    "美股 sleeve 更弱：US 組合 lockbox CAGR -7.4%（FAIL），US 清單僅 informational，不建議照單操作。",
    "每日持股 = 因子配方套用在『今日抓到的 universe』，為 indicative 籃子，非 universe-pinned 回測"
    "重現、亦非買賣訊號；CAGR/回撤才是 universe-pinned。",
    "報酬含 survivorship 上界（以現行成分回測整段歷史），真實 point-in-time 數字會較低。",
]

METHOD = {
    "sleeves": ["lowvol", "strev", "mom"],
    "blend": "inverse-vol（反波動加權，低相關 sleeve 分散）",
    "rebalance": "per-sleeve（lowvol/strev 月、mom 季）",
}


def read_track_record(path):
    """Read optimize_<sleeve>.json["combo"] → flat dict the lens displays, or None if the file is
    missing / corrupt / has no combo block (graceful — never raises). NEVER recomputes the gate."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("SKIP validated track record %s: %s", path, e)
        return None
    combo = (data or {}).get("combo")
    if not combo:
        return None
    lock = combo.get("lockbox") or {}
    return {
        "cagr": lock.get("cagr"),
        "max_dd": lock.get("max_dd"),
        "calmar": lock.get("calmar"),
        "dsr": combo.get("dsr"), "dsr_pass": combo.get("dsr_pass"),
        "pbo": combo.get("pbo"), "pbo_pass": combo.get("pbo_pass"),
        "spa_p": combo.get("spa_p"), "spa_pass": combo.get("spa_pass"),
        "lockbox_pass": combo.get("lockbox_pass"),
        "flat_lift": combo.get("flat_lift"), "flat_pass": combo.get("flat_pass"),
        "overall_pass": combo.get("pass"),          # the honest bottom line (False)
        "n_trials": combo.get("n_trials"),
        "n_search": combo.get("n_search"), "n_lock": combo.get("n_lock"),
        "sleeves": combo.get("sleeves"),
        "combo_configs": combo.get("combo_configs") or {},
        "period": data.get("period"),
    }


def compute_daily_targets(prices, combo_configs, names=None):
    """Apply the pre-registered combo recipe to TODAY's universe: for each sleeve, score every
    name with the sleeve's factor (run_optimize.factor_panel), take the LAST row (today), pick the
    sleeve's top-N (backtest_portfolio.select_top_n); union across sleeves, tagged by which sleeve
    picked each. Returns [{ticker, name, sleeves, weight, price}] (equal-weight indicative basket),
    or [] on empty/short input. Pure: input frames never mutated; only data <= today is used."""
    names = names or {}
    if not prices:
        return []
    try:
        _open, close = bp.build_panels(prices)
    except Exception as e:
        log.warning("SKIP daily targets (build_panels): %s", e)
        return []
    if close is None or close.empty:
        return []
    picks = {}                                       # ticker -> set(sleeves)
    for sleeve, cfg in (combo_configs or {}).items():
        family = cfg.get("family", sleeve)
        lookback = int(cfg.get("lookback", 60))
        top_n = int(cfg.get("top_n", 20))
        try:
            panel = ro.factor_panel(family, close, lookback)
        except Exception:
            continue
        if panel is None or len(panel) == 0:
            continue
        for ticker in bp.select_top_n(panel.iloc[-1], top_n):
            picks.setdefault(ticker, set()).add(sleeve)
    if not picks:
        return []
    weight = round(1.0 / len(picks), 4)              # equal-weight indicative basket
    out = []
    for ticker, sleeves in picks.items():
        price = None
        try:
            price = round(float(close[ticker].dropna().iloc[-1]), 2)
        except Exception:
            price = None
        out.append({"ticker": ticker, "name": names.get(ticker) or ticker,
                    "sleeves": sorted(sleeves), "weight": weight, "price": price})
    out.sort(key=lambda r: (-len(r["sleeves"]), r["ticker"]))   # consensus picks first
    return out


def passive_benchmark(index_history, label=None):
    """15y CAGR + MaxDD of a passive index (0050 / SPY) — the recommended core benchmark.
    Pure over a single OHLCV frame; None on missing/too-short history."""
    if index_history is None or "Close" not in getattr(index_history, "columns", []):
        return None
    c = index_history["Close"].dropna()
    if len(c) < 2:
        return None
    years = max((c.index[-1] - c.index[0]).days / 365.25, 1e-9)
    cagr = (float(c.iloc[-1]) / float(c.iloc[0])) ** (1.0 / years) - 1.0
    max_dd = float((c / c.cummax() - 1.0).min())
    return {"label": label, "cagr": round(cagr, 4), "max_dd": round(max_dd, 4),
            "start": str(c.index[0].date()), "end": str(c.index[-1].date())}


def build_track(tw_histories, us_histories, optimize_tw_json, optimize_us_json,
                tw_index=None, us_index=None, tw_names=None, us_names=None):
    """Assemble the 驗證組合 track dict for the PWA payload / report. Each sleeve degrades
    independently (missing JSON → track_record None, empty universe → [] holdings; never raises).
    JSON-serializable throughout. Mirrors momentum_portfolio.build_lens."""
    tw_tr = read_track_record(optimize_tw_json)
    us_tr = read_track_record(optimize_us_json)
    cfg_tw = (tw_tr or {}).get("combo_configs") or {}
    cfg_us = (us_tr or {}).get("combo_configs") or cfg_tw
    return {
        "tw": {
            "holdings": compute_daily_targets(tw_histories, cfg_tw, names=tw_names),
            "track_record": tw_tr,
        },
        "us": {
            "holdings": compute_daily_targets(us_histories, cfg_us, names=us_names),
            "track_record": us_tr,
        },
        "passive_benchmark": {
            "tw": passive_benchmark(tw_index, label="0050"),
            "us": passive_benchmark(us_index, label="SPY"),
        },
        "method": dict(METHOD),
        "disclaimers": list(DISCLAIMERS),
    }
