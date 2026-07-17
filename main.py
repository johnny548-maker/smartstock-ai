# -*- coding: utf-8 -*-
"""SmartStock Daily AI System — orchestrator.

Pipeline: market data + news + 法人籌碼 → 選股打分 → 規則點評
          → 風險引擎 + 資產配置 + 再平衡 → 報告檔 + Email。

Every external stage is wrapped: a failure is logged as SKIP and the run
continues with whatever data is available (never a silent drop)."""
import argparse
import glob
import json
import logging
import math
import os
from datetime import datetime

import config
import data_fetcher
import news_digest
import institutional
import strategy
import ai_analyzer
import levels as levels_mod
import asset_allocation
import rebalance
import report_builder
import notifier_file
import notifier_email
import web_export
import chip_state
import delta as delta_mod
import calendar_events
import breadth as breadth_mod
import revenue as revenue_mod
import theme as theme_mod
import signals as signals_mod
import universe as universe_mod
import edgar as edgar_mod
import verdict as verdict_mod
import market_regime as regime_mod
import correlation as correlation_mod
import earnings_guard as earnings_mod
import short_volume as shortvol_mod
import macro
import fx_context as fx_mod
import fundamentals
import trends as trends_mod
import watchlist_tracker
import stock_detail
import overlay_snapshot
import radar_outcomes
import market_panel
import pick_outcomes
import strategy_health as strategy_health_mod
import shadow_portfolio as shadow_mod
import data_health as data_health_mod
import positions as positions_mod
import us_market
import pretrade as pretrade_mod
import peer_valuation as peerval_mod
import attribution as attribution_mod
import indicators
import supply_chain
import momentum_portfolio as momentum_mod
import validated_portfolio as validated_mod
import risk_lens

# sources/ overlay framework (keyless informational overlays — OVERLAY-NOT-SCORER).
# Each fetcher is injectable + graceful-skip; the wiring below guards every source
# independently so a dead source SKIPs without aborting the run, and NOTHING here
# ever feeds strategy.rank_stocks / score_stock (golden-additive invariant).
from sources import overlay as _overlay
from sources import twse as _twse
from sources import tpex as _tpex
from sources import tdcc as _tdcc
from sources import sec as _sec
# P2 keyless overlay/environment producers (same OVERLAY-NOT-SCORER contract). Market/
# sector-level (macro_tw/taifex/macro_us) expose to_environment() -> a flat dict of named
# gauges (NOT keyed by ticker) merged into a separate 'environment' payload section; the
# per-stock producers (sec_frames/openfda) expose to_overlays() -> {ticker:[overlay]} that
# MERGE into the existing overlays_map beside chip/法人. NONE of these touch the scoring call.
from sources import macro_tw as _macro_tw
from sources import taifex as _taifex
from sources import macro_us as _macro_us
from sources import sec_frames as _sec_frames
from sources import openfda as _openfda
from sources import notice as _notice
# P3 keyless news/catalyst/sentiment/attention/flows overlay producers (same OVERLAY-NOT-
# SCORER contract). PER-STOCK producers expose to_overlays() -> {ticker:[overlay]} merged
# into overlays_map (news catalyst/sentiment, wiki/HN attention, SEC FTD chip). SECTOR/MARKET
# producers expose to_environment()/cot_sector_tilt() -> merged into the 'environment' dict
# under environment['sector_tilt']. NONE of these touch the scoring call (ranked is final).
from sources import news_catalyst as _news_catalyst
from sources import altdata as _altdata
from sources import sec_flows as _sec_flows


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def load_portfolio_state():
    """Load current holdings weights. Non-numeric keys (e.g. _comment) dropped."""
    try:
        with open(config.PORTFOLIO_STATE, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(v, (int, float))}
    except Exception:
        return {}


def run_stage(log, skips, name, fn, default=None, msg=None):
    """B2: run a fail-open SINGLE-FETCH pipeline stage. On any exception, log 'SKIP <msg>:
    <err>', record `name` in skips, and return `default` — the run continues, never a silent
    drop (payload['skips'] is later set()-deduped). Used ONLY for the simple single-assignment
    stages; multi-statement stages with their own side effects / nested guards / in-try logging
    / non-appending excepts stay EXPLICIT (the fail-open is intentionally visible per-site)."""
    try:
        return fn()
    except Exception as e:
        log.warning("SKIP %s: %s", msg or name, e)
        skips.append(name)
        return default


def main(web=False, dry_run=False, date_arg=None):
    setup_logging()
    log = logging.getLogger("main")
    if date_arg is not None:
        # Validate format eagerly so a bad --date fails fast with a clear error.
        from datetime import datetime as _dt
        _dt.strptime(date_arg, "%Y-%m-%d")   # raises ValueError on bad format
        date_str = date_arg
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
    skips = []
    log.info("=== SmartStock daily run %s ===", date_str)

    # 1. Market context + signal --------------------------------------------
    frames, indices = run_stage(log, skips, "indices", data_fetcher.get_market_context,
                                default=({}, {}), msg="market context")
    signal = run_stage(log, skips, "signal",
                       lambda: data_fetcher.build_market_signal(frames, indices),
                       default={"risk": "LOW"}, msg="market signal")
    risk = signal.get("risk", "LOW")

    # 2. News ----------------------------------------------------------------
    news = run_stage(log, skips, "news", news_digest.get_news, default={})

    # 2b. Market breadth (broad basket → 參與度) ----------------------------
    breadth = run_stage(log, skips, "breadth", breadth_mod.get_breadth, default=None)
    if not breadth:
        skips.append("breadth")

    # 2b-fx. FX dimension (USD/TWD spot context, B9) — DISPLAY-ONLY overlay, never
    #     scored (no backtest gate; the Wilson-CI gate is only for weighted signals).
    try:
        fx = fx_mod.get_fx()
    except Exception as e:
        log.warning("SKIP fx: %s", e); fx = None
    if not fx:
        skips.append("fx")

    # 2c. 月營收早期成長候選 (全上市一次掃描，keyless leading spine) ---------
    revenue_data = run_stage(log, skips, "revenue", revenue_mod.get_early_candidates, default=None)

    # 2d. 主題湧現 (news-driven theme rotation → 供應鏈 tickers) --------------
    themes = run_stage(log, skips, "themes", lambda: theme_mod.get_themes(news), default=[])

    # 2e. 機會掃描 (全市場早期領導股，watchlist 外的小型成長股 — Round 2 P0-A) -----
    try:
        opp = universe_mod.get_opportunities()
        cmap = edgar_mod.ticker_to_cik()                       # enrich US leaders w/ SEC 季營收
        for ld in opp.get("leaders", []):
            if not ld["ticker"].endswith((".TW", ".TWO")):
                g = edgar_mod.revenue_growth(ld["ticker"], cik_map=cmap)
                if g:
                    ld["rev_yoy"] = g.get("yoy"); ld["rev_accel"] = g.get("accel")
        log.info("opportunity scan: %d universe, %d scanned, %d leaders",
                 opp["universe"], opp["scanned"], len(opp["leaders"]))
    except Exception as e:
        log.warning("SKIP opportunity scan: %s", e); opp = None; skips.append("opportunity")

    # 2e-mom. 動能組合（季度）lens — quarterly top-20 12-1 momentum PORTFOLIO view.
    #     Decision 2026-06-13: momentum is a PORTFOLIO-CONSTRUCTION factor (rank+hold,
    #     proven by backtest_portfolio.py), NOT a daily explosive signal (event-study
    #     lift 0.89 < 1 → vetoed from score_stock). So it's a SEPARATE lens that
    #     consumes the opportunity universe's ALREADY-FETCHED OHLCV (NO extra network)
    #     + reads the committed backtest_portfolio_*.json track record (never recomputes).
    #     OVERLAY-NOT-SCORER: nothing here touches strategy.score_stock / rank_stocks.
    #     FAIL-OPEN: a lens failure SKIPs to an empty lens, never aborts the daily report.
    momentum_lens = {}
    try:
        _mom_data = (opp or {}).get("_data") or {} if isinstance(opp, dict) else {}
        _tw_hist = {t: df for t, df in _mom_data.items() if t.endswith((".TW", ".TWO"))}
        _us_hist = {t: df for t, df in _mom_data.items() if not t.endswith((".TW", ".TWO"))}
        _opp_names = {}
        for _ld in (opp or {}).get("leaders", []):
            if _ld.get("ticker") and _ld.get("name"):
                _opp_names.setdefault(_ld["ticker"], _ld["name"])
        _here = os.path.dirname(os.path.abspath(__file__))
        momentum_lens = momentum_mod.build_lens(
            _tw_hist, _us_hist,
            os.path.join(_here, "backtest_portfolio_tw.json"),
            os.path.join(_here, "backtest_portfolio_us.json"),
            tw_names={**config.STOCK_NAMES, **_opp_names},
            us_names={**config.STOCK_NAMES, **_opp_names})
        log.info("momentum lens: TW %d holding(s), US %d holding(s)",
                 len(momentum_lens.get("tw", {}).get("holdings", [])),
                 len(momentum_lens.get("us", {}).get("holdings", [])))
    except Exception as e:
        log.warning("SKIP momentum lens: %s", e); momentum_lens = {}; skips.append("momentum_portfolio")

    # 2e-val. 驗證組合 track — the 15y-validated risk-MANAGED combo (LOWVOL+STREV+MOM inverse-vol)
    #     surfaced alongside the PASSIVE 0050/SPY benchmark. Reads optimize_<sleeve>.json["combo"]
    #     (the rigorous-gate output: PASSES DSR/PBO/lockbox ~10.7%/-21% TW, FAILS SPA-vs-index
    #     p=0.17 → overall not-pass). Same NO-extra-network pattern as the momentum lens (reuses the
    #     opp universe OHLCV + the already-fetched index frames as the 0050/SPY proxy). HONEST: does
    #     NOT and is NOT claimed to beat the index. OVERLAY-NOT-SCORER. FAIL-OPEN → empty on error.
    validated_lens = {}
    try:
        _vt_data = (opp or {}).get("_data") or {} if isinstance(opp, dict) else {}
        _vt_tw = {t: df for t, df in _vt_data.items() if t.endswith((".TW", ".TWO"))}
        _vt_us = {t: df for t, df in _vt_data.items() if not t.endswith((".TW", ".TWO"))}
        _vt_names = {**config.STOCK_NAMES}
        for _ld in (opp or {}).get("leaders", []):
            if _ld.get("ticker") and _ld.get("name"):
                _vt_names.setdefault(_ld["ticker"], _ld["name"])
        _vt_here = os.path.dirname(os.path.abspath(__file__))
        validated_lens = validated_mod.build_track(
            _vt_tw, _vt_us,
            os.path.join(_vt_here, "optimize_tw.json"),
            os.path.join(_vt_here, "optimize_us.json"),
            tw_index=frames.get("twii"), us_index=frames.get("sp500"),
            tw_names=_vt_names, us_names=_vt_names)
        log.info("validated lens: TW %d holding(s), US %d holding(s), overall_pass=%s",
                 len(validated_lens.get("tw", {}).get("holdings", [])),
                 len(validated_lens.get("us", {}).get("holdings", [])),
                 (validated_lens.get("tw", {}).get("track_record") or {}).get("overall_pass"))
    except Exception as e:
        log.warning("SKIP validated lens: %s", e); validated_lens = {}; skips.append("validated_portfolio")

    # 2e-fic. 每因子 15y 驗證信心 — surface each base factor's full-universe 15y cross-sectional
    #     rank-IC (committed docs/data/_factor_ic_state.json) so the PWA can show the daily picks
    #     ARE 15y-validated AND the HONEST truth that every base factor is weak (IC<0.05). Read-only,
    #     additive; no scorer change. FAIL-OPEN → empty.
    factor_validation = {}
    try:
        _fic_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "docs", "data", "_factor_ic_state.json")
        with open(_fic_path, encoding="utf-8") as _ff:
            _fic = json.load(_ff)
        factor_validation = {k: {"rank_ic": v.get("rank_ic"), "edge": v.get("edge")}
                             for k, v in (_fic.get("families") or {}).items()}
    except Exception as e:
        log.warning("SKIP factor_validation: %s", e); skips.append("factor_validation")

    # 3. 三大法人 ------------------------------------------------------------
    inst = run_stage(log, skips, "institutional",
                     lambda: institutional.get_institutional(config.STOCKS_TW), default={})
    if not inst:
        skips.append("institutional")

    # 4. Stock data + ranking ------------------------------------------------
    all_syms = config.STOCKS_TW + config.STOCKS_US
    data = run_stage(log, skips, "stock_data",
                     lambda: data_fetcher.get_stock_data(all_syms), default={}, msg="stock data")
    # 4a. 籌碼 cross-run buffer: record today (trading days only), derive chips
    chips_state = chip_state.load()
    # 4a-fund. Fundamentals overlay cache + revenue state (no network; B12). The
    #     revenue STATE (full YoY buffer) is loaded separately from revenue_data
    #     (today's candidate list) because build_badge needs the per-code YoY history.
    fund_cache = fundamentals.load_cache()
    rev_state = run_stage(log, skips, "fundamentals", revenue_mod.load_state,
                          default=None, msg="revenue state load")
    if inst:
        for sym in config.STOCKS_TW:
            di = inst.get(sym.replace(".TW", "")) or {}
            df = data.get(sym)
            volu = int(df["Volume"].iloc[-1]) if df is not None and len(df) else 0
            if di:
                chip_state.update(chips_state, sym, date_str,
                                  di.get("foreign", 0), di.get("trust", 0), volu)
        chip_state.save(chips_state)
    chips_map = {sym: chip_state.chips_for(chips_state, sym) for sym in all_syms}

    ranked = strategy.rank_stocks(data, institutional_map=inst, frames=frames, chips_map=chips_map)
    log.info("ranked %d / %d symbols", len(ranked), len(all_syms))

    # 4b. 風險透鏡 — 每檔 beta/相關 vs 指數（OVERLAY-NOT-SCORER，純警示，不進評分）。把「可信選股=
    #     集中高 beta ≈ 槓桿版指數」這個 session 教訓直接顯示在卡上。fail-open。
    try:
        for _it in ranked[:config.DISPLAY_N]:
            _bench, _bname = risk_lens.bench_for(_it["stock"], frames)
            _b = risk_lens.beta_to_bench(data.get(_it["stock"]), _bench, bench_name=_bname)
            if _b:
                _it["beta_60"], _it["corr_idx"] = _b["beta"], _b["corr"]
                _it["beta_bench"] = _b["benchmark"]
                _it["beta_low_corr"] = _b["low_corr"]
                _it["beta_n"] = _b["n"]
    except Exception as e:
        log.warning("SKIP beta lens: %s", e)

    # 4c. 早期訊號雷達 (RS線新高/安靜吸籌/型態 gated on 月營收/主題) — informational
    try:
        rev_codes = [c["code"] for c in (revenue_data or {}).get("candidates", [])]
        hot_tix = theme_mod.hot_tickers(themes)
        sig = signals_mod.scan_signals(data, frames=frames, chips_map=chips_map,
                                       revenue_codes=rev_codes, theme_tickers=hot_tix,
                                       names=config.STOCK_NAMES)
        log.info("early signals: %d on board", len(sig["board"]))
    except Exception as e:
        log.warning("SKIP signals: %s", e); sig = {"per_stock": {}, "board": []}; skips.append("signals")

    # 4b. Today's movers (basket sorted by today's % change) ----------------
    movers = []
    for sym, df in data.items():
        try:
            if len(df) >= 2:
                pct = (df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1) * 100
                if math.isfinite(pct):
                    movers.append({"stock": sym, "pct": round(float(pct), 2)})
        except Exception:
            continue
    movers.sort(key=lambda m: m["pct"], reverse=True)

    # 5. Commentary + ATR price levels for the Top N ------------------------
    analyses, level_map = {}, {}
    for item in ranked[:config.TOP_N]:
        df = data.get(item["stock"])
        lv = levels_mod.compute_levels(df) if df is not None else None
        level_map[item["stock"]] = lv
        analyses[item["stock"]] = ai_analyzer.analyze_stock(
            item["stock"], item["score"], item["factors"], item.get("sector"), levels=lv)

    # 5b. Per-pick card enrichment (燈號/verdict/量比/S-R/趨勢序列) for the PWA ----
    #     B12: attach an informational fundamentals badge (TW 月營收 YoY / US PE-EPS).
    #     OVERLAY-NOT-SCORER — a badge failure must SKIP, never abort the run.
    pick_cards = {}
    fund_attached = 0
    for item in ranked[:config.DISPLAY_N]:
        sym = item["stock"]
        badge = None
        try:
            badge = fundamentals.build_badge(
                sym, rev_state=rev_state, fund_cache=fund_cache,
                is_tw=sym.endswith((".TW", ".TWO")))
            if badge:
                fund_attached += 1
        except Exception as e:
            log.warning("SKIP fundamental badge %s: %s", sym, e)
        pick_cards[sym] = verdict_mod.enrich(
            item["stock"], item["score"], item["factors"],
            data.get(item["stock"]), level_map.get(item["stock"]), fundamental=badge)
    try:
        fundamentals.save_cache(fund_cache)        # persist any US PE/EPS fetched/cached
        if fund_attached:
            log.info("fundamentals: %d pick badge(s) attached", fund_attached)
    except Exception as e:
        log.warning("SKIP fundamentals cache save: %s", e); skips.append("fundamentals")

    # 5c. Risk overlay (analyst G1/G2/G10): market-regime gate + concentration ---
    try:
        regime = regime_mod.market_regime(frames)
        if regime:
            log.info("market regime: %s (%d%% exposure)", regime["label"], regime["exposure"])
    except Exception as e:
        log.warning("SKIP regime: %s", e); regime = None
    # 5c-macro. FRED macro spine (B6): yield-curve / credit / NFCI RISK-CONTEXT
    #     OVERLAY — informational backdrop only, NEVER summed into risk or any
    #     stock score (要做回測才加權). The live ^VIX/^TNX risk input is untouched.
    try:
        macro_ctx = macro.macro_context(cache_path=config.MACRO_CACHE)
        if macro_ctx:
            log.info("macro overlay: %s (flags: %s)",
                     macro_ctx.get("label"), ", ".join(macro_ctx.get("flags") or []) or "—")
    except Exception as e:
        log.warning("SKIP macro: %s", e); macro_ctx = None; skips.append("macro")
    try:
        pick_data = {it["stock"]: data[it["stock"]] for it in ranked[:config.DISPLAY_N]
                     if data.get(it["stock"]) is not None}
        concentration = correlation_mod.concentration(pick_data, names=config.STOCK_NAMES)
        conc_summary = risk_lens.sector_concentration(
            ranked[:config.DISPLAY_N], sector_map=getattr(config, "SECTOR_MAP", {}),
            top_n=config.DISPLAY_N, conc_data=pick_data, names=config.STOCK_NAMES)
    except Exception as e:
        log.warning("SKIP concentration: %s", e); concentration = None; conc_summary = {}

    # 5d. Earnings-blackout overlay (analyst G5): flag picks with a binary earnings
    #     event in the next 7d — INFORMATIONAL only, never a score change.
    ecache = os.path.join(config.WEB_DIR, "data", "_earnings_cache.json")
    earnings_bo = {}
    try:
        earnings_bo = earnings_mod.annotate(
            [it["stock"] for it in ranked[:config.DISPLAY_N]], cache_path=ecache)
        for sym, b in earnings_bo.items():
            if sym in pick_cards:
                pick_cards[sym]["earnings"] = b
        if earnings_bo:
            log.info("earnings blackout: %d pick(s) within %dd", len(earnings_bo),
                     earnings_mod.WITHIN_DAYS)
        # 30d forewarning (de-scoped补): attach earn_watch (7<d≤30 volatility band) to picks not
        # already in the hard 7d blackout. Reuses the cache annotate() just refreshed. Rides the
        # card via web_export's **card spread. INFORMATIONAL — never a score change.
        earn_cal = earnings_mod.annotate_calendar(
            [it["stock"] for it in ranked[:config.DISPLAY_N]], cache_path=ecache)
        for sym, c in earn_cal.items():
            if sym in pick_cards and c.get("watch_vol") and "earnings" not in pick_cards[sym]:
                pick_cards[sym]["earn_watch"] = c
    except Exception as e:
        log.warning("SKIP earnings guard: %s", e)

    # 5d-pos. 我的持倉 (P2-S1): a holdings-aware overnight-risk lens BESIDE the daily picks.
    #     OVERLAY-NOT-SCORER / INFORMATIONAL: every output here is a SUGGESTION the user
    #     applies by hand — suggested_stop is DISPLAY-ONLY and is NEVER written back to the
    #     ledger; nothing here touches strategy.score_stock / rank_stocks. The ledger lives
    #     in docs/data/_positions_state.json (schema v2, populated out-of-band by the Sheet
    #     read-back step `sheets_sync.py --pull-positions`). SKIP-not-abort: any failure logs
    #     a warning, records the skip, and leaves my_positions absent — the run continues.
    my_positions = None
    pos_evals = []
    try:
        _pos_path = os.path.join(config.WEB_DIR, "data", "_positions_state.json")
        _pos_raw = positions_mod.load(_pos_path)
        _pos_state, _pos_errs = positions_mod.validate(_pos_raw)
        for _pe in _pos_errs:                    # bad rows → surfaced in the skips report
            log.warning("SKIP position row: %s", _pe); skips.append("positions:" + _pe)
        _held_syms = [p["symbol"] for p in _pos_state.get("positions", [])]
        if _held_syms:
            # Held names not in today's universe → batch-fetch their OHLCV so they can be
            # evaluated (pick_outcomes._default_fetch idiom: keyed by the held symbol; it
            # already maps .TW/.TWO/US via yahoo_symbol — the same normaliser positions
            # used, so the keys line up). A dead fetch degrades to a null-price eval (graceful).
            _pos_price = {s: data.get(s) for s in _held_syms if data.get(s) is not None}
            _missing = [s for s in _held_syms if s not in _pos_price]
            if _missing:
                try:
                    from datetime import timedelta as _td
                    _end = datetime.now()
                    _start = _end - _td(days=120)
                    _fetched = pick_outcomes._default_fetch(
                        _missing, _start.strftime("%Y-%m-%d"), _end.strftime("%Y-%m-%d"))
                    _pos_price.update({s: df for s, df in (_fetched or {}).items()
                                       if df is not None})
                    log.info("positions: fetched %d/%d held name(s) outside universe",
                             len(_fetched or {}), len(_missing))
                except Exception as _fe:
                    log.warning("SKIP positions universe-extend fetch: %s", _fe)
            # earnings blackout for HELD names (reuse the SAME on-disk earnings cache — no
            # extra fetch budget for names already annotated above).
            try:
                _held_earn = earnings_mod.annotate(_held_syms, cache_path=ecache)
            except Exception as _ee:
                log.warning("SKIP positions earnings annotate: %s", _ee); _held_earn = {}
            _clusters = (concentration or {}).get("clusters") if isinstance(concentration, dict) else None
            pos_evals = positions_mod.evaluate_positions(
                _pos_state, _pos_price, indicators.atr, _held_earn, _clusters)
            my_positions = positions_mod.summarize(_pos_state, pos_evals)
            log.info("my_positions: %d held, %d alert(s)",
                     len(_pos_state.get("positions", [])), my_positions.get("alert_count", 0))
    except Exception as e:
        log.warning("SKIP my_positions: %s", e); skips.append("my_positions")

    # 5d-pre. Pre-trade checklist (M3): synthesise five EXISTING gates (regime / earnings
    #     blackout / cluster crowding / liquidity / R:R) into a glanceable yes/no card BESIDE
    #     each pick. OVERLAY-NOT-SCORER: pretrade.build_checklist reads shapes already produced
    #     above (regime / earnings_bo / concentration / verdict.risk+liquidity) — ZERO new
    #     signals, ZERO scoring. card['pretrade'] is a sidecar; score/factors untouched. The
    #     whole block is SKIP-not-abort. Cluster count per pick = how many of today's DISPLAY_N
    #     picks share its highly-correlated cluster (correlation.concentration clusters carry
    #     'tickers' keyed by the FULL pick symbol).
    try:
        _clusters = (concentration or {}).get("clusters") if isinstance(concentration, dict) else []
        _cluster_count = {}              # pick symbol -> # of same-cluster picks (incl. itself)
        for _cl in (_clusters or []):
            _tix = _cl.get("tickers") or []
            for _t in _tix:
                _cluster_count[_t] = len(_tix)
        _pretrade_n = 0
        for _item in ranked[:config.DISPLAY_N]:
            _sym = _item["stock"]
            _card = pick_cards.get(_sym)
            if not _card:
                continue
            _risk = _card.get("risk") or {}
            _liq = _card.get("liquidity") or {}
            # Merge the verdict.liquidity + risk_sizing.plan shapes into the single risk_plan
            # dict the pretrade gates read: liq_thin (verdict.liquidity 'thin'), size_ceiling
            # (risk_sizing 'size_ceiling_pct', else the liquidity position cap), rr (risk 'rr').
            _risk_plan = {
                "liq_thin": _liq.get("thin"),
                "size_ceiling": _risk.get("size_ceiling_pct", _liq.get("cap")),
                "rr": _risk.get("rr"),
            }
            try:
                _card["pretrade"] = pretrade_mod.build_checklist(
                    _sym, regime, _cluster_count.get(_sym), _risk_plan, earnings_bo.get(_sym))
                _pretrade_n += 1
            except Exception as _pte:
                log.warning("SKIP pretrade %s: %s", _sym, _pte)
        if _pretrade_n:
            log.info("pretrade checklist: %d pick card(s)", _pretrade_n)
    except Exception as e:
        log.warning("SKIP pretrade: %s", e); skips.append("pretrade")

    # 5e. FINRA RegSHO short-volume overlay (B5): flag US picks with elevated/extreme
    #     daily short-volume ratio — INFORMATIONAL only, never a score change (要做回測
    #     才加權). US-only: TW has no keyless daily short-VOLUME equivalent (融券 is a
    #     balance, not volume) → logged FALLBACK, never silently dropped.
    shortvol_board = []
    try:
        sv_state = shortvol_mod.load_cache()
        sv = shortvol_mod.fetch_short_volume(symbols=set(config.STOCKS_US))
        if sv.get("rows"):
            shortvol_mod.update_cache(sv_state, sv["date"], sv["rows"])
            shortvol_mod.save_cache(sv_state)
        log.info("short_volume FALLBACK: TW skipped (no keyless daily short-VOLUME; "
                 "融券 is a balance, not volume) — US-only overlay")
        sv_pick_syms = [it["stock"] for it in ranked[:config.DISPLAY_N]]
        shortvol_map = shortvol_mod.build_overlay(sv_state, sv_pick_syms)
        for sym, ov in shortvol_map.items():
            if sym in pick_cards:
                pick_cards[sym]["shortvol"] = ov
        # board list (flagged names) for the payload summary block
        for sym, ov in shortvol_map.items():
            if ov.get("flag"):
                shortvol_board.append({
                    "stock": sym, "name": config.STOCK_NAMES.get(sym),
                    "pct": ov["pct"], "flag": ov["flag"],
                    "rising": ov["rising"], "days": ov["days"], "note": ov["note"]})
        shortvol_board.sort(key=lambda x: x["pct"], reverse=True)
        if shortvol_board:
            log.info("short_volume: %d US pick(s) flagged", len(shortvol_board))
    except Exception as e:
        log.warning("SKIP short_volume: %s", e); skips.append("short_volume")

    # 6. Allocation + rebalance ---------------------------------------------
    base = asset_allocation.base_allocation()
    target = asset_allocation.adjust_allocation(base, signal)
    current = load_portfolio_state()
    reb = rebalance.rebalance(current, target) if current else {}

    # 6b. Calendar (本周注意) + yesterday delta -----------------------------
    try:
        events = calendar_events.upcoming_events([it["stock"] for it in ranked[:config.TOP_N]])
    except Exception as e:
        log.warning("SKIP calendar: %s", e); events = []
    delta_changes = delta_mod.compute_delta(
        {"picks": ranked, "risk": risk, "institutional": inst},
        delta_mod.load_prev(date_str))

    # 7. Build the report ----------------------------------------------------
    markdown = report_builder.build_report(
        date_str=date_str, news=news, indices=indices, institutional=inst,
        ranked=ranked, analyses=analyses, allocation=target,
        rebalance_diff=reb, risk=risk, movers=movers,
        delta=delta_changes, events=events, breadth=breadth, revenue=revenue_data,
        signals=sig, themes=themes, opportunity=opp, regime=regime,
        concentration=concentration, macro=macro_ctx,
        momentum_portfolio=momentum_lens)
    # NOTE: validated_portfolio / factor_validation are build_PAYLOAD-only kwargs — build_REPORT
    # (the markdown/email path) does not accept them. Passing them here crashed every daily run
    # (latent: unit tests never invoke main()); caught by the local live run 2026-06-23.

    # 7b. Continuous watchlist tracker (REQ3b) — enroll today's picks, re-evaluate every
    #     tracked name against today's OHLCV, persist. INFORMATIONAL board only — never an
    #     order, never a score input. SKIP-not-abort: a failure must not break the run.
    #     pins=[] server-side (user pins live in browser localStorage; client pins-to-top).
    wl_board = []
    try:
        wl_path = os.path.join(config.WEB_DIR, "data", "_watchlist_state.json")
        wl = watchlist_tracker.load(wl_path)
        # CRITICAL FIX: rank_stocks results carry NO 'price' key, so enroll() used to
        # store entry_price=0.0 for every new name (17 historical zeros in _watchlist_state).
        # Resolve a REAL entry price per pick via the pick_outcomes fallback idiom
        #   pick['price'] → df last close → levels.entry → 0.0
        # and pass it as 'price' (enroll reads pick['price']). Immutable: each pick is a copy,
        # the ranked items are never mutated. Existing 17 zeros are NOT backfilled (honest scar).
        enroll_picks = [
            {**it, "price": watchlist_tracker.resolve_entry_price(
                it, data.get(it["stock"]), level_map.get(it["stock"]))}
            for it in ranked[:config.DISPLAY_N]
        ]
        watchlist_tracker.enroll(wl, enroll_picks, pins=[], date=date_str)
        watchlist_tracker.reevaluate(wl, data, frames, date_str)
        watchlist_tracker.save(wl, wl_path)
        wl_board = watchlist_tracker.board(wl)
        log.info("watchlist: %d tracked name(s) on board", len(wl_board))
    except Exception as e:
        log.warning("SKIP watchlist tracker: %s", e); skips.append("watchlist")

    # 7c. Per-stock detail files (REQ1 long-tail) — standalone JSON so revenue candidates
    #     and other displayed names open a usable detail view in the PWA WITHOUT a new
    #     network fetch in the cron hot path. SKIP-not-abort.
    try:
        details = {}

        # B2: Revenue candidates — batch-fetch real OHLCV so charts render.
        # Falls back to df=None (metadata-only) if the fetch fails entirely.
        rev_candidates = (revenue_data or {}).get("candidates", [])
        rev_codes = [c["code"] for c in rev_candidates if c.get("code")]
        rev_ohlcv = {}
        if rev_codes:
            try:
                rev_ohlcv = universe_mod.fetch_revenue_ohlcv(rev_codes, period=config.OPP_PERIOD)
                if rev_ohlcv:
                    log.info("revenue OHLCV: %d/%d candidates fetched", len(rev_ohlcv), len(rev_codes))
            except Exception as e:
                log.warning("SKIP revenue OHLCV batch (falling back to metadata-only): %s", e)

        # 雷達/全市場精選 stocks must carry the SAME 基本面 dict as picks (rev_yoy + accel) so
        # the detail sheet reaches parity. TW codes resolve via the buffered revenue state (no
        # new network); US opp names get their fundamentals from overlays (sec_frames/PE), not here.
        def _opp_fund(code):
            if not (isinstance(code, str) and code.endswith((".TW", ".TWO"))):
                return None
            try:
                return fundamentals.build_badge(
                    code, rev_state=rev_state, fund_cache=fund_cache, is_tw=True)
            except Exception:
                return None

        for c in rev_candidates:
            code = c.get("code")
            if not code:
                continue
            try:
                badge = fundamentals.build_badge(
                    code, rev_state=rev_state, fund_cache=fund_cache, is_tw=True)
            except Exception:
                badge = None
            # Use real df if available from the batch fetch, else metadata-only.
            # Explicit is-None check avoids ValueError when get() returns a DataFrame
            # (pandas raises "The truth value of a DataFrame is ambiguous" on `or`).
            rev_df = rev_ohlcv.get(code)
            if rev_df is None:
                rev_df = rev_ohlcv.get(code + ".TW")
            details[code] = stock_detail.build_detail(
                code, df=rev_df, name=c.get("name"), fundamental=badge)

        # Picks: a standalone file each (df + levels in hand) so a deep-link always resolves.
        for item in ranked[:config.DISPLAY_N]:
            sym = item["stock"]
            if sym in details:
                continue
            details[sym] = stock_detail.build_detail(
                sym, df=data.get(sym), name=config.STOCK_NAMES.get(sym),
                fundamental=(pick_cards.get(sym) or {}).get("fundamental"),
                levels=level_map.get(sym))

        # A3: Opportunity leaders + breakout candidates — build detail with real OHLCV.
        # The opp data dict was populated by fetch_opportunity_ohlcv_robust inside
        # get_opportunities(), but is not threaded out to main.py directly.  We access
        # it via the already-computed opp result: if the ticker's df is in the payload
        # (ohlc bars are attached to leaders/breakout when ready), we reconstruct it;
        # otherwise we ask for a lightweight re-fetch via the SAME universe data already
        # downloaded by get_opportunities — use opp_data threaded below.
        if opp is not None:
            opp_data_ref = (opp or {}).get("_data", {}) or {}
            for ld in (opp or {}).get("leaders", []):
                ticker = ld.get("ticker")
                if not ticker or ticker in details:
                    continue
                df_opp = opp_data_ref.get(ticker)
                details[ticker] = stock_detail.build_detail(
                    ticker, df=df_opp, name=ld.get("name"), fundamental=_opp_fund(ticker))
            for bc in (opp or {}).get("breakout", []):
                ticker = bc.get("stock") or bc.get("ticker")
                if not ticker or ticker in details:
                    continue
                df_opp = opp_data_ref.get(ticker)
                details[ticker] = stock_detail.build_detail(
                    ticker, df=df_opp, name=bc.get("name"), fundamental=_opp_fund(ticker))

        if details:
            written = stock_detail.export_details(details, config.WEB_DIR)
            log.info("detail files: %d written (picks=%d, revenue=%d, opp=%d)",
                     len(written),
                     len([k for k in details if k in {it["stock"] for it in ranked[:config.DISPLAY_N]}]),
                     len([k for k in details if k in set(rev_codes)]),
                     len([k for k in details
                          if k not in {it["stock"] for it in ranked[:config.DISPLAY_N]}
                          and k not in set(rev_codes)]))
    except Exception as e:
        log.warning("SKIP detail files: %s", e); skips.append("detail_files")

    # 7c-score. Fix 1 (GAP C): score the WIDE opportunity universe (~600 names) through the
    #     SAME backtest-gated rank_stocks formula as the 28-name core board, and surface a
    #     全市場精選 'scored_universe' board. Resource-thin small-caps simply don't fire the
    #     chip/法人 factors (graceful None in score_stock) → they rank lower, not penalised.
    #     Runs BEFORE the opp['_data'] pop below (consumes the already-fetched OHLCV — NO extra
    #     network). OVERLAY-NOT-SCORER for the core picks: this is a SEPARATE board; `ranked`
    #     above is already final and untouched. SKIP-not-abort: a failure → empty board.
    scored_universe = []
    # #3 full-market verdict map {code: {s: score, l: light}} for EVERY scored name, so the
    #   all-market search shows a current recommendation (買入≥90 / 觀望40-89 / 不持有<40).
    #   Seeded with the core picks; the opportunity universe + (over time) the keyless panel
    #   widen it. OVERLAY-NOT-SCORER: a display map, never feeds scoring.
    verdict_map = {it["stock"]: {"s": it["score"], "l": verdict_mod.light(it["score"])}
                   for it in ranked}
    try:
        _opp_data = (opp or {}).get("_data") or {} if isinstance(opp, dict) else {}
        if _opp_data:
            _core_syms = {it["stock"] for it in ranked}
            _opp_ranked = strategy.rank_stocks(_opp_data, institutional_map=inst, frames=frames)
            # Audit finding 4: this call passes NO chips_map (and opp names have no
            # sector), so these rows are scored on a REDUCED input set vs the core
            # board → 板塊外 verdicts are capped at 觀察 (never 🟢 買入) via the
            # row-level inputs_complete flag rank_stocks now carries.
            for _sr in _opp_ranked:
                verdict_map.setdefault(
                    _sr["stock"],
                    verdict_mod.capped_entry(_sr["score"],
                                             _sr.get("inputs_complete", False)))
            # #1 fix: back-fill the Chinese NAME (rank_stocks rows carry none) from the
            # opportunity-universe names map (universe.get_opportunities now exports it),
            # falling back to the 28-name STOCK_NAMES. Also enrich price + 1-day %chg from the
            # already-fetched OHLCV so the PWA board + radar ledger have a price. Graceful per row.
            _opp_names = (opp or {}).get("names") or {}
            for _r in _opp_ranked:
                if not _r.get("name"):
                    _r["name"] = _opp_names.get(_r["stock"]) or config.STOCK_NAMES.get(_r["stock"])
                if not _r.get("light") and _r.get("score") is not None:
                    _r["light"] = verdict_mod.light(_r["score"])   # self-name the verdict light
                # Audit finding 4: 板塊外 board rows (no chips/sector inputs) never show
                # 🟢 — cap at 觀察 + carry the partial reason for the PWA chips row.
                if not _r.get("inputs_complete", True):
                    if _r.get("light") == "green":
                        _r["light"] = "amber"
                    _r["partial_inputs"] = True
                    _r["partial_reason"] = verdict_mod.PARTIAL_REASON
                _rdf = _opp_data.get(_r["stock"])
                if _rdf is not None and not getattr(_rdf, "empty", True) and len(_rdf) >= 1:
                    try:
                        _c = float(_rdf["Close"].iloc[-1]); _r["price"] = round(_c, 2)
                        if len(_rdf) >= 2:
                            _p = float(_rdf["Close"].iloc[-2])
                            if _p > 0:
                                _r["change_pct"] = round((_c / _p - 1) * 100, 2)
                    except Exception:
                        pass
            scored_universe = web_export.select_scored_universe(
                _opp_ranked, exclude=_core_syms, top_n=config.SCORED_UNIVERSE_N)
            log.info("scored universe: %d ranked, %d on board",
                     len(_opp_ranked), len(scored_universe))
    except Exception as e:
        log.warning("SKIP scored universe: %s", e); skips.append("scored_universe")

    # Chart-for-every-stock: write a per-stock detail file (K-line OHLC) for EVERY opportunity-
    #   universe name from the already-fetched OHLCV — so clicking ANY 機會/全市場精選/動能/雷達
    #   stock (not just the core picks) shows its 技術線圖. Reuses the live `details` dict, which is
    #   exported with the rest at the overlay-attach step (~line 1159). SKIP-not-abort.
    try:
        _opp_frames = (opp or {}).get("_data") or {} if isinstance(opp, dict) else {}
        _opp_nm = (opp or {}).get("names") or {}
        _n_chart = 0
        for _csym, _cdf in _opp_frames.items():
            # dedup against the BARE form too: section 7c writes revenue details under a bare code
            # ('2344'), so writing '2344.TW' here would land a byte-identical duplicate file. app.js
            # resolves bare↔.TW on click, so one canonical file suffices.
            _cbare = _csym.replace(".TWO", "").replace(".TW", "")
            if _csym in details or _cbare in details or _cdf is None or getattr(_cdf, "empty", True):
                continue
            details[_csym] = stock_detail.build_detail(
                _csym, df=_cdf, name=_opp_nm.get(_csym) or config.STOCK_NAMES.get(_csym),
                fundamental=_opp_fund(_csym))
            _n_chart += 1
        log.info("opportunity chart detail files: +%d", _n_chart)
    except Exception as e:
        log.warning("SKIP opportunity detail files: %s", e)

    # itemC: full-US-market verdict coverage. No keyless bulk OHLC exists for US, so a ROTATING
    #   ~500-name batch of the full Nasdaq Trader directory (~5653 common stocks) is scored each
    #   run through the SAME gated rank_stocks and accumulated in a persistent store → full
    #   coverage cycles in ~12 days. The whole store merges into verdict_map so the all-market
    #   search shows a US verdict. SKIP-not-abort; set env US_COVERAGE=0 to disable.
    try:
        if os.environ.get("US_COVERAGE", "1") != "0":
            _us_syms, _us_names = universe_mod.us_full_market()
            if _us_syms:
                _batch = us_market.rotating_slice(
                    _us_syms, datetime.now().toordinal(), config.US_COVERAGE_BATCH)
                _us_frames = us_market.fetch_batch(_batch)
                _fresh = us_market.score_batch(
                    _us_frames, (frames or {}).get("sp500"), names=_us_names,
                    nasdaq_frame=(frames or {}).get("nasdaq"))   # #3: US benches vs ^IXIC, not ^GSPC
                # Chart-for-every-stock: write a detail file per US batch stock (frames already
                #   fetched) so each US verdict is clickable with its K-line. Same `details` dict.
                try:
                    for _usym, _udf in (_us_frames or {}).items():
                        if _usym in details or _udf is None or getattr(_udf, "empty", True):
                            continue
                        details[_usym] = stock_detail.build_detail(
                            _usym, df=_udf, name=_us_names.get(_usym))
                except Exception as _de:
                    log.warning("SKIP US detail files: %s", _de)
                _store = us_market.merge_store(
                    us_market.load_store(config.US_VERDICTS_STATE), _fresh, date_str)
                us_market.save_store(config.US_VERDICTS_STATE, _store)
                for _sym, _v in _store.items():
                    # Audit finding 4: itemC US coverage scores on frames only (no
                    # sector/inst/chips) → same partial-inputs cap as the opp scan.
                    _s = _v.get("s")
                    if _s is None:
                        verdict_map.setdefault(_sym, {"s": _s, "l": _v.get("l")})
                    else:
                        verdict_map.setdefault(
                            _sym, verdict_mod.capped_entry(_s, inputs_complete=False))
                log.info("US coverage: batch=%d fresh=%d store=%d",
                         len(_batch), len(_fresh), len(_store))
    except Exception as e:
        log.warning("SKIP US coverage: %s", e); skips.append("us_coverage")

    # #16: flush the opportunity + US detail-file additions to disk NOW with their own export.
    #   They were added to the shared `details` dict AFTER the early export (line ~590); the only
    #   other flush is the late re-export inside the overlay-attach try, so a non-NameError there
    #   would silently strand EVERY opp/US/leader chart (→ '技術線圖尚未抓取' on click). Idempotent
    #   (re-writing the already-flushed picks is harmless). Mirrors the panel block's own flush.
    try:
        log.info("opp/US chart detail files flushed: %d total",
                 len(stock_detail.export_details(details, config.WEB_DIR)))   # returns a list, not int
    except Exception as _fe:
        log.warning("SKIP opp/US detail flush: %s", _fe)

    # P2 item8: holdings ↔ verdict second-pass. Positions were evaluated early (line ~386,
    #   before verdict_map existed). Now that verdict_map (core + opportunity universe) is
    #   built, flag any HELD stock whose current verdict turned 'red' (不持有). OVERLAY-NOT-
    #   SCORER: appends an informational alert only. SKIP-not-abort.
    try:
        if my_positions:
            my_positions = positions_mod.merge_verdict_alerts(my_positions, verdict_map)
    except Exception as e:
        log.warning("SKIP holdings↔verdict merge: %s", e)

    # Belt-and-suspenders: drop the heavy OHLCV frames from opp now that the
    # detail-file loop (A3) has consumed them.  web_export.build_payload also
    # strips '_data', but releasing the DataFrames here avoids keeping ~600×N
    # frames alive in memory for the rest of the run.
    if isinstance(opp, dict):
        opp.pop("_data", None)

    # 7d. Promote the early/breakout 起漲 board out of the opportunity result so the PWA
    #     gets it as a top-level key too. SKIP-not-abort.
    early_board = []
    try:
        early_board = (opp or {}).get("breakout", []) or []
    except Exception as e:
        log.warning("SKIP early board promote: %s", e); skips.append("early_board")

    # 7e. sources/ overlay framework — keyless chip/法人/基本面/內部人 OVERLAYS.
    #     OVERLAY-NOT-SCORER: nothing here touches the scoring call (ranked is already
    #     final above); these are INFORMATIONAL overlays attached BESIDE the cards via
    #     overlay.attach (immutable — returns NEW dicts, never mutates pick_cards/details).
    #     Each source is independently try/except-guarded → a dead source SKIPs the run.
    #     overlays_map {symbol -> [overlay]} is also threaded into the PWA payload, and a
    #     source_coverage map records which sources returned data today (counts).
    overlays_map = {}            # symbol/code -> merged list[overlay]
    source_coverage = {}         # source name -> {"ok": bool, "codes": int, "overlays": int}

    # #7: record the opportunity-OHLCV scan + US verdict batch coverage so a Yahoo rate-limit
    #   episode that collapses the wide universe DEGRADES health (visible in the banner) instead
    #   of shipping silent 'ok' — data_health._check_sources reads every source_coverage entry.
    try:
        _opp_scanned = (opp or {}).get("scanned", 0) if isinstance(opp, dict) else 0
        source_coverage["opp_ohlcv"] = {
            "ok": _opp_scanned >= int(config.OPP_SCAN_LIMIT * 0.5), "codes": _opp_scanned}
    except Exception:
        pass
    try:
        source_coverage["us_batch"] = {"ok": bool(_fresh), "codes": len(_fresh or {})}
    except NameError:
        pass                     # US coverage disabled / SKIPped → no entry
    except Exception:
        pass

    def _merge_overlays(per_code, source_name):
        """Merge a {code -> [overlay]} map (from a fetcher's to_overlays) into the
        running overlays_map and record coverage. Pure dict assembly — never raises
        on a None/odd shape (the caller's try/except is the SKIP boundary)."""
        codes = 0
        n_ov = 0
        for code, ovs in (per_code or {}).items():
            if not ovs:
                continue
            overlays_map.setdefault(code, [])
            overlays_map[code].extend(ovs)
            codes += 1
            n_ov += len(ovs)
        source_coverage[source_name] = {"ok": codes > 0, "codes": codes, "overlays": n_ov}

    # symbols of interest = the displayed picks (bare TWSE code + full symbol both kept so
    # a {2330 -> ...} overlay map resolves to a 2330.TW card and vice-versa).
    # Fix 4 (GAP D): WIDEN coverage beyond the ~12 displayed picks to the radar cohort —
    #     opportunity leaders + the Fix-1 scored_universe — so the whole-market keyless data
    #     we already fetch (BWIBBU PE / T86 三大法人 / 融資券 margin / FINRA short%) attaches to
    #     MANY more names instead of being thrown away. The fetch is already whole-market; this
    #     only widens the to_overlays_* `symbols` filter. OVERLAY-NOT-SCORER: informational only.
    _pick_syms = [it["stock"] for it in ranked[:config.DISPLAY_N]]
    _overlay_syms = list(_pick_syms)
    _overlay_syms += [ld.get("ticker") for ld in (opp or {}).get("leaders", [])
                      if isinstance(ld, dict) and ld.get("ticker")]
    _overlay_syms += [r.get("stock") for r in (scored_universe or []) if r.get("stock")]
    _tw_codes = {s.replace(".TWO", "").replace(".TW", "") for s in _overlay_syms
                 if s and s.endswith((".TW", ".TWO"))}
    _us_syms = {s for s in _overlay_syms if s and not s.endswith((".TW", ".TWO"))}

    # --- TWSE 上市 chip/法人/基本面 (T86 三大法人 + MI_MARGN 融資融券 + BWIBBU PE) -----
    try:
        t86_rows = _twse.fetch_t86()
        # T86 daily snapshot → archive for net_buy_streak history (one file per trading day)
        if t86_rows:
            try:
                parsed = [r for r in (_twse.parse_t86_row(x) for x in t86_rows) if r]
                _twse_archive = os.path.join(config.ARCHIVE_DIR, "t86")
                _cache_date = date_str.replace("-", "")
                from sources import _cache as _src_cache
                _src_cache.archive_snapshot(_twse_archive, _cache_date, parsed)
            except Exception as e:
                log.warning("SKIP T86 snapshot archive: %s", e)
        _merge_overlays(_twse.to_overlays_t86(t86_rows, symbols=_tw_codes, as_of=date_str),
                        "twse_t86")
        margin_rows = _twse.fetch_margin()
        _merge_overlays(_twse.to_overlays_margin(margin_rows, symbols=_tw_codes, as_of=date_str),
                        "twse_margin")
        pe_rows = _twse.fetch_pe()
        _merge_overlays(_twse.to_overlays_pe(pe_rows, symbols=_tw_codes), "twse_pe")
    except Exception as e:
        log.warning("SKIP twse overlays: %s", e); skips.append("twse_overlays")

    # --- TPEx 上櫃 mirror (3insti + margin + PE) — OTC chip intelligence ----------------
    try:
        insti_metrics = _tpex.to_3insti_metrics(_tpex.fetch_tpex_3insti())
        margin_metrics = _tpex.to_margin_metrics(_tpex.fetch_tpex_margin())
        pe_metrics = _tpex.to_pe_metrics(_tpex.fetch_tpex_pe())
        _merge_overlays(
            _tpex.to_overlays(insti_metrics=insti_metrics, margin_metrics=margin_metrics,
                              pe_metrics=pe_metrics, as_of=date_str),
            "tpex")
    except Exception as e:
        log.warning("SKIP tpex overlays: %s", e); skips.append("tpex_overlays")

    # --- W2: TWSE 注意股 / 處置股 regulatory-flag overlays (kind='risk') -----------------
    #     Per-code risk badges (⚠️注意股 / 🚫處置股第N次) beside any flagged pick. Independent
    #     SKIP boundary: a dead TWSE endpoint never aborts the run. OVERLAY-NOT-SCORER.
    try:
        notice_map = _notice.fetch_notice_stocks()
        _merge_overlays(_notice.to_overlays_notice(notice_map, as_of=date_str), "twse_notice")
        disposition_map = _notice.fetch_disposition_stocks()
        _merge_overlays(_notice.to_overlays_disposition(disposition_map, as_of=date_str),
                        "twse_punish")
    except Exception as e:
        log.warning("SKIP notice/disposition overlays: %s", e); skips.append("notice_overlays")

    # --- W4: 融券佔流通股% per-stock overlay (MI_MARGN 融券餘額 ÷ 已發行普通股數) ----------
    #     Pairs the existing MI_MARGN 融券數據 with a t187ap03_L float map to compute the
    #     short-interest ratio. Independent fetch (own margin pull) so this SKIPs in isolation.
    #     OVERLAY-NOT-SCORER: informational chip badge; needs a Wilson-CI backtest before weight.
    try:
        _short_margin_rows = _twse.fetch_margin()
        _float_map = _twse.build_float_map(_twse.fetch_t187ap03_l())
        _merge_overlays(
            _twse.to_overlays_short_pct(_short_margin_rows, _float_map, as_of=date_str),
            "twse_short")
    except Exception as e:
        log.warning("SKIP short_pct overlays: %s", e); skips.append("short_pct_overlays")

    # --- TDCC 集保戶股權分散 (weekly 大戶集中度) — save weekly archive for WoW trend ----
    try:
        tdcc_rows = _tdcc.fetch_distribution()
        if tdcc_rows:
            # 資料日期 (AD YYYYMMDD) is the archive key; a same-week re-pull overwrites.
            _tdcc_date = (tdcc_rows[0].get("date") or date_str.replace("-", "")).strip()
            _tdcc_arch_dir = os.path.join(config.ARCHIVE_DIR, "tdcc")
            try:
                _tdcc.save_weekly(tdcc_rows, _tdcc_date, archive_dir=_tdcc_arch_dir)
            except Exception as e:
                log.warning("SKIP tdcc weekly archive: %s", e)
            # last week's rows (if accrued) → WoW 大戶吸籌/散戶化 verdict, else snapshot-only.
            # Only the single most-recent PRIOR week is needed — read that one file (gzip-aware)
            # instead of merging every ~6.4MB weekly snapshot in the archive.
            _prior_key = _tdcc.prior_week_key(_tdcc_date, archive_dir=_tdcc_arch_dir)
            last_week_rows = _tdcc.load_week(_prior_key, archive_dir=_tdcc_arch_dir) \
                if _prior_key else None
            _merge_overlays(
                _tdcc.to_overlays(tdcc_rows, last_week_rows=last_week_rows,
                                  codes=_tw_codes or None, as_of=_tdcc_date),
                "tdcc")
        else:
            source_coverage["tdcc"] = {"ok": False, "codes": 0, "overlays": 0}
    except Exception as e:
        log.warning("SKIP tdcc overlays: %s", e); skips.append("tdcc_overlays")

    # --- SEC EDGAR Form-4 內部人交易 (US picks only) ------------------------------------
    try:
        if _us_syms:
            _t2c, _c2t = _sec._build_ticker_cik()
            idx_rows, _sec_idx_date = _sec.fetch_recent_daily_index(date=date_str.replace("-", ""))
            _sec_as_of = _sec_idx_date if _sec_idx_date else date_str
            form4 = _sec.form4_filings_today(idx_rows)
            records_by_issuer = {}
            for f in form4:
                cik = f.get("cik")
                tkr = _sec.ticker_for_cik(cik, _maps=(_t2c, _c2t))
                if tkr is None or tkr not in _us_syms:
                    continue
                try:
                    xml_text = _sec._real_fetch(
                        "https://www.sec.gov/Archives/" + f["path"])
                    rec = _sec.parse_form4(xml_text)
                except Exception:
                    continue
                records_by_issuer.setdefault(tkr, []).append(rec)
            _merge_overlays(_sec.to_overlays(records_by_issuer, as_of=_sec_as_of), "sec")
            source_coverage["sec"] = {**source_coverage.get("sec", {}), "as_of": _sec_as_of}
        else:
            source_coverage["sec"] = {"ok": False, "codes": 0, "overlays": 0}
    except Exception as e:
        log.warning("SKIP sec overlays: %s", e); skips.append("sec_overlays")

    # 7e-P2. sources/ P2 keyless overlays — market/sector ENVIRONMENT + extra per-stock
    #     overlays. Same OVERLAY-NOT-SCORER contract: nothing here touches the scoring call
    #     (ranked is already final). Each source is INDEPENDENTLY try/except-guarded → a dead
    #     source SKIPs without aborting. Market-level sources expose to_environment() (a flat
    #     dict of named gauges, NOT keyed by ticker) → merged into a single 'environment' dict
    #     {regime, industry, macro}; per-stock sources (sec_frames/openfda) expose to_overlays()
    #     → {ticker:[overlay]} merged into overlays_map (same path as P1 via _merge_overlays).
    import time as _time
    from sources import _cache as _src_cache_p2
    environment = {}             # {regime:{...}, industry:{...}, macro:{...}} (market-level)
    _now_ts = _time.time()

    # --- TAIFEX index-level regime (外資台指期淨未平倉 + Put/Call ratio) -------------------
    try:
        _inst_rows = _taifex.fetch_inst_futures()
        _pcr_rows = _taifex.fetch_put_call_ratio()
        _regime_env = _taifex.to_environment(_inst_rows, _pcr_rows, as_of=date_str)
        environment["regime"] = _regime_env
        source_coverage["taifex"] = {
            "ok": bool(_inst_rows or _pcr_rows),
            "keys": len([k for k, v in _regime_env.items() if v is not None and k != "note"]),
        }
    except Exception as e:
        log.warning("SKIP taifex environment: %s", e); skips.append("taifex_env")

    # --- W4: TPEx 上櫃現股當沖市場統計 — MARKET-WIDE daytrade ratio (NOT per-stock) -----------
    #     A single market-level gauge (當沖佔成交量%, 投機熱 when > threshold) → environment
    #     section, mirroring the taifex regime placement (NOT keyed by ticker, NEVER scored).
    try:
        _daytrade = _tpex.to_daytrade_overlay(
            _tpex.parse_daytrade_rows(_tpex.fetch_tpex_daytrade()))
        if _daytrade:
            environment["tpex_daytrade"] = _daytrade
        source_coverage["tpex_daytrade"] = {"ok": bool(_daytrade), "keys": 1 if _daytrade else 0}
    except Exception as e:
        log.warning("SKIP tpex_daytrade environment: %s", e); skips.append("tpex_daytrade_env")

    # --- macro_tw industry/sector environment (外銷訂單→出貨→產出 + 景氣對策信號) — 24h cache -
    try:
        def _fetch_tw_env():
            cycle_rows = _macro_tw.fetch_business_cycle_signal()
            # export orders: try MOEA direct HTML first (keyless, verified 2026-06-07);
            # fall back to data.gov.tw dataset path if DATASET_EXPORT_ORDERS is pinned.
            export_rows = _macro_tw.fetch_export_orders_moea() or _macro_tw.fetch_export_orders()
            # IPI: try MOEA GA direct HTML (detail table first for 電子零組件業).
            ipi_rows = _macro_tw.fetch_industrial_production_moea() or _macro_tw.fetch_industrial_production()
            semi_hs_rows = _macro_tw.fetch_customs_hs(_macro_tw.SEMI_HS_CODE)
            return _macro_tw.to_environment(
                export_rows=export_rows, ipi_rows=ipi_rows,
                cycle_rows=cycle_rows, semi_hs_rows=semi_hs_rows)
        _industry_env = _src_cache_p2.cached_fetch(
            config.ENV_TW_CACHE, "macro_tw_env", 24 * 3600, _now_ts, _fetch_tw_env)
        if _industry_env:
            environment["industry"] = _industry_env
            # de-scoped补 D-B: honest MACRO-level electronics/semi cycle-momentum gauge (NOT per-
            # stock). Pure derivation from the industry env; OVERLAY-NOT-SCORER. fail-open.
            try:
                _cyc = risk_lens.electronics_cycle_momentum(_industry_env)
                if _cyc.get("state"):
                    environment["electronics_cycle"] = _cyc
            except Exception as _ce:
                log.warning("SKIP electronics_cycle: %s", _ce)
            _ig = [k for k, v in _industry_env.items()
                   if k != "meta" and v not in (None, {"light": None, "score": None})]
            source_coverage["macro_tw"] = {"ok": bool(_ig), "keys": len(_ig)}
        else:
            source_coverage["macro_tw"] = {"ok": False, "keys": 0}
    except Exception as e:
        log.warning("SKIP macro_tw environment: %s", e); skips.append("macro_tw_env")

    # --- macro_us environment (BLS CPI/PPI YoY + Treasury USD/TWD book rate) — 24h cache ---
    try:
        def _fetch_us_env():
            return _macro_us.to_environment()
        _macro_env = _src_cache_p2.cached_fetch(
            config.ENV_US_CACHE, "macro_us_env", 24 * 3600, _now_ts, _fetch_us_env)
        if _macro_env:
            environment["macro"] = _macro_env
            _mg = [k for k in ("cpi_yoy", "ppi_yoy", "usd_twd") if _macro_env.get(k) is not None]
            source_coverage["macro_us"] = {"ok": bool(_mg), "keys": len(_mg)}
        else:
            source_coverage["macro_us"] = {"ok": False, "keys": 0}
    except Exception as e:
        log.warning("SKIP macro_us environment: %s", e); skips.append("macro_us_env")

    # --- SEC XBRL frames per-stock US fundamentals (Revenues/NetIncomeLoss) overlays -------
    try:
        if _us_syms:
            _t2c2, _c2t2 = _sec._build_ticker_cik()
            # W3: pull the EXTENDED concept set (adds StockholdersEquity / AssetsCurrent /
            # LiabilitiesCurrent / GrossProfit / CostOfRevenue) so build_fundamentals_index
            # derives roe / current_ratio / gross_margin onto each slot; to_overlays carries
            # those ratios into the overlay value (None → omitted). INFORMATIONAL — never scored.
            _frames_index = _sec_frames.build_fundamentals_index(
                concepts=_sec_frames.EXTENDED_CONCEPTS, fetch_fn=_sec._real_fetch)
            _merge_overlays(
                _sec_frames.to_overlays(_frames_index, _c2t2, as_of=date_str), "sec_frames")
        else:
            source_coverage["sec_frames"] = {"ok": False, "codes": 0, "overlays": 0}
    except Exception as e:
        log.warning("SKIP sec_frames overlays: %s", e); skips.append("sec_frames_overlays")

    # --- peer (同業) valuation percentile overlays (M4) — where a name sits vs its peers ----
    #     ZERO new data source: a RECOMBINATION of feeds already pulled today. US = the SEC
    #     cross-section (sec_frames) joined to an injected yfinance market-cap batch → PS/PE/ROE
    #     percentile within a peer cross-section; TW = the BWIBBU PE/PB/DY rows REGROUPED by the
    #     supply_chain theme (same dimension group_rs uses) → percentile within theme peers. The
    #     US cross-section is cached 30d (PEERVAL_CACHE_PATH) per the daily-cron budget. n<MIN_GROUP
    #     groups emit nothing (honest). OVERLAY-NOT-SCORER: make_overlay(kind='fundamental',
    #     severity='info') dicts merged into overlays_map via the same _merge_overlays path —
    #     never scored. SKIP-not-abort.
    try:
        # US: cross-section over today's US picks, market caps via a thin yfinance fast_info batch.
        if _us_syms:
            _pv_t2c, _pv_c2t = _sec._build_ticker_cik()

            def _pv_mktcap(tickers):
                """Injected market-cap batch: {ticker: marketCap float|None} via yfinance
                fast_info (graceful per-name; a missing cap → that name's PS/PE is None)."""
                import yfinance as _yf
                caps = {}
                for _t in tickers:
                    try:
                        _fi = _yf.Ticker(_t).fast_info
                        _mc = _fi.get("market_cap") if hasattr(_fi, "get") else getattr(_fi, "market_cap", None)
                        caps[_t] = float(_mc) if _mc else None
                    except Exception:
                        caps[_t] = None
                return caps

            _pv_us = peerval_mod.us_cross_section(
                fetch_fn=_sec._real_fetch, mktcap_fn=_pv_mktcap, cik_to_ticker=_pv_c2t)
            _pv_us_ov = peerval_mod.to_overlays_us(
                _pv_us, group_label="US picks", metric="ps", as_of=date_str)
            _merge_overlays(_pv_us_ov, "peer_valuation_us")
        # TW: BWIBBU PE rows regrouped by supply_chain theme → percentile within theme peers.
        _pv_pe_rows = [r for r in (_twse.parse_pe_row(x) for x in (_twse.fetch_pe() or [])) if r]
        _pv_theme_map = {}
        for _r in _pv_pe_rows:
            _code = str(_r.get("code", "")).strip()
            if not _code:
                continue
            _theme, _ = supply_chain.ticker_theme(_code)
            if _theme:
                _pv_theme_map[_code] = _theme
        _pv_groups = peerval_mod.tw_groups(_pv_pe_rows, _pv_theme_map)
        _merge_overlays(peerval_mod.to_overlays(_pv_groups, metric="pe", as_of=date_str),
                        "peer_valuation_tw")
    except Exception as e:
        log.warning("SKIP peer_valuation overlays: %s", e); skips.append("peer_valuation_overlays")

    # --- openFDA drug approval/recall catalyst overlays (US pharma picks, sponsor-mapped) --
    try:
        # Curated sponsor->ticker map keyed off the watchlist's pharma/biotech names (narrow
        # by design — see openfda.py docstring on the sponsor-name join pain). The current
        # 28-name watchlist holds no pharma, so this stays silent today (graceful, no spurious
        # fire) yet wires the source so a future pharma pick lights up with zero code change.
        _sponsor_map = getattr(config, "OPENFDA_SPONSOR_MAP", None) or {}
        if _sponsor_map:
            _approvals = _openfda.fetch_recent_approvals(since_days=30)
            _recalls = _openfda.fetch_recent_recalls(since_days=30)
            _merge_overlays(
                _openfda.to_overlays(_approvals, _recalls, _sponsor_map, as_of=date_str),
                "openfda")
        else:
            source_coverage["openfda"] = {"ok": False, "codes": 0, "overlays": 0}
    except Exception as e:
        log.warning("SKIP openfda overlays: %s", e); skips.append("openfda_overlays")

    # 7e-P3. sources/ P3 keyless news/catalyst/sentiment/attention/flows OVERLAYS. Same
    #     OVERLAY-NOT-SCORER contract: nothing here touches the scoring call (ranked is final).
    #     Each source is INDEPENDENTLY try/except-guarded → a dead/429 source SKIPs without
    #     aborting. PER-STOCK producers (news catalyst/sentiment, wiki/HN attention, SEC FTD)
    #     merge into overlays_map via _merge_overlays; the SECTOR-level CFTC COT producer merges
    #     into environment['sector_tilt']. News/alt fetches are cached (cached_fetch TTL) so the
    #     daily cron stays off the live (rate-limited) endpoints.
    #
    #     Name map for tagless TW feeds (Yahoo-TW/CNA/UDN have no per-item ticker) + for the
    #     revenue/leader display names: config.STOCK_NAMES plus today's revenue-candidate +
    #     opportunity-leader names (mirrors web_export._names_map so a headline about a
    #     revenue/leader name still resolves to its code).
    _p3_name_map = dict(config.STOCK_NAMES)
    for _c in (revenue_data or {}).get("candidates", []):
        _code, _nm = _c.get("code"), _c.get("name")
        if _code and _nm:
            _p3_name_map.setdefault(_code, _nm)
    for _ld in (opp or {}).get("leaders", []):
        _code, _nm = _ld.get("ticker"), _ld.get("name")
        if _code and _nm:
            _p3_name_map.setdefault(_code, _nm)

    # --- news_catalyst: multi-source keyless news → catalyst/sentiment per-stock overlays -----
    #     Fetch (cached 2h — news moves intraday but a daily cron only needs one pull), normalize,
    #     dedup across sources, map tagless feeds onto card names, emit {ticker:[overlay]}.
    try:
        _news_cache = os.path.join(config.WEB_DIR, "data", "_news_catalyst_cache.json")

        def _fetch_news_raw():
            _nc = _news_catalyst
            # cnYES (ticker-tagged) + tagless TW feeds + Yahoo-US per-pick + GDELT(reuters proxy).
            _items = []
            for _raw in _nc.fetch_cnyes():
                _it = _nc.normalize_item(_raw, _nc.SRC_CNYES)
                if _it:
                    _items.append(_it)
            for _src, _fetch in ((_nc.SRC_YAHOO_TW, _nc.fetch_yahoo_tw),
                                 (_nc.SRC_CNA, _nc.fetch_cna),
                                 (_nc.SRC_UDN, _nc.fetch_udn)):
                for _raw in _fetch():
                    _it = _nc.normalize_item(_raw, _src)
                    if _it:
                        # tagless feed → best-effort name→ticker map onto a card.
                        if not _it.get("ticker"):
                            _tk = _nc.map_headline_to_ticker(_it.get("title"), _p3_name_map)
                            if _tk:
                                _bare = _tk.replace(".TWO", "").replace(".TW", "")
                                _it = {**_it, "ticker": _bare, "tickers": [_bare]}
                        _items.append(_it)
            for _sym in _us_syms:
                for _raw in _nc.fetch_yahoo_us(_sym):
                    _it = _nc.normalize_item(_raw, _nc.SRC_YAHOO_US)
                    if _it:
                        _items.append({**_it, "ticker": _sym, "tickers": [_sym]})
            # serialise to plain dicts for the cache (cached_fetch JSON round-trips).
            return _items

        _news_items = _src_cache_p2.cached_fetch(
            _news_cache, "news_catalyst_items", 2 * 3600, _now_ts, _fetch_news_raw) or []
        _deduped = _news_catalyst.dedup_catalysts(_news_items)
        _merge_overlays(_news_catalyst.to_overlays(_deduped, as_of=date_str), "news_catalyst")
    except Exception as e:
        log.warning("SKIP news_catalyst overlays: %s", e); skips.append("news_catalyst_overlays")

    # --- altdata: Wikipedia pageviews + Hacker News attention sentiment overlays ----------------
    #     Per pick with a mappable wiki title (any) / HN tech allow-list. Cached 6h (attention is
    #     slow-moving relative to a daily report; keeps the cron off Wikimedia/Algolia).
    try:
        _alt_cache = os.path.join(config.WEB_DIR, "data", "_altdata_cache.json")
        _alt_syms = [it["stock"] for it in ranked[:config.DISPLAY_N]]

        def _fetch_alt_raw():
            _ad = _altdata
            _out = {}        # symbol -> {"wiki": [...], "hn": [...]}
            for _sym in _alt_syms:
                _bare = _sym.replace(".TWO", "").replace(".TW", "")
                _wiki = []
                _hn = []
                _wmap = _ad.TICKER_WIKITITLE.get(_sym.upper()) or _ad.TICKER_WIKITITLE.get(_bare)
                if _wmap:
                    _title, _proj = _wmap
                    _wiki = _ad.fetch_wiki_pageviews(_title, project=_proj)
                if _sym.upper() in _ad.HN_TECH_TICKERS:
                    _hn = _ad.fetch_hn(_sym.upper())
                if _wiki or _hn:
                    _out[_sym] = {"wiki": _wiki, "hn": _hn}
            return _out

        _alt_raw = _src_cache_p2.cached_fetch(
            _alt_cache, "altdata_attention", 6 * 3600, _now_ts, _fetch_alt_raw) or {}
        for _sym, _bundle in _alt_raw.items():
            _merge_overlays(
                _altdata.to_overlays(_sym, _bundle.get("wiki") or [], _bundle.get("hn") or [],
                                     as_of=date_str, now_ts=_now_ts),
                "altdata")
    except Exception as e:
        log.warning("SKIP altdata overlays: %s", e); skips.append("altdata_overlays")

    # --- sec_flows: SEC FTD per-stock chip overlays + CFTC COT sector-tilt environment ----------
    #     FTD = US-only (the file carries the ticker; scope to US picks). COT = SECTOR/MARKET-level
    #     → environment['sector_tilt'] (NOT per-ticker). Both informational, needs_backtest.
    try:
        _ftd_rows = _sec_flows.fetch_ftd()
        # scope the FTD overlay to the US picks (symbol_map = {US_SYMBOL: US_SYMBOL}) so an
        # unrelated heavily-failed name never lights up a card not in today's universe.
        _ftd_symmap = {s: s for s in _us_syms} if _us_syms else None
        _merge_overlays(_sec_flows.to_overlays(_ftd_rows, symbol_map=_ftd_symmap, as_of=date_str),
                        "sec_ftd")
    except Exception as e:
        log.warning("SKIP sec_ftd overlays: %s", e); skips.append("sec_ftd_overlays")

    try:
        _cot_rows = _sec_flows.fetch_cot()
        _cot_tilt = _sec_flows.cot_sector_tilt(_cot_rows)
        # SECTOR/MARKET-level: surface under environment['sector_tilt'] (NOT per-ticker, NOT scored).
        environment["sector_tilt"] = _cot_tilt
        source_coverage["cftc_cot"] = {"ok": bool(_cot_tilt), "keys": len(_cot_tilt)}
    except Exception as e:
        log.warning("SKIP cftc_cot environment: %s", e); skips.append("cftc_cot_env")

    if environment:
        log.info("environment gauges: %s",
                 ", ".join("%s:%s" % (k, "ok" if v else "—") for k, v in environment.items()))

    # Attach the merged overlays onto pick_cards + details (immutable — NEW dicts).
    # A symbol resolves an overlay list under its full form OR its bare TWSE code.
    def _overlays_for(symbol):
        out = list(overlays_map.get(symbol, []))
        bare = symbol.replace(".TWO", "").replace(".TW", "")
        if bare != symbol:
            out += overlays_map.get(bare, [])
        return out

    try:
        n_attached = 0
        for sym in list(pick_cards.keys()):
            ovs = _overlays_for(sym)
            if ovs:
                pick_cards[sym] = _overlay.attach(pick_cards[sym], ovs)
                n_attached += 1
        # details may be keyed by bare codes (revenue candidates) or full symbols (picks).
        try:
            # Load + index the T86 daily and TDCC weekly archives ONCE for the whole detail
            # loop (hoist): build_trends previously re-loaded + re-scanned both archives per
            # code (O(codes × rows)); preload_indices makes it O(rows + codes).
            _trends_preloaded = trends_mod.preload_indices() if details else None
            for code in list(details.keys()):
                ovs = _overlays_for(code)
                if ovs:
                    details[code] = _overlay.attach(details[code], ovs)
                # 籌碼/基本面 history series — same INFORMATIONAL sidecar as overlays; attach to
                # every detail (US/thin → empty → key omitted) so the sheet draws a trend, not
                # just a single same-day number. build_trends is fully guarded (never raises).
                try:
                    _tr = trends_mod.build_trends(code, rev_state=rev_state,
                                                  preloaded=_trends_preloaded)
                    if any(_tr.get(k) for k in ("inst_cum", "holder_pct", "rev_yoy")):
                        details[code]["trends"] = _tr
                except Exception:
                    pass
            # re-export the detail files so the attached overlays land in the per-stock JSON.
            if details:
                stock_detail.export_details(details, config.WEB_DIR)
        except NameError:
            pass    # details may not exist if section 7c SKIPped
        if overlays_map:
            log.info("source overlays: %d code(s), %d attached to picks; coverage=%s",
                     len(overlays_map), n_attached,
                     ", ".join("%s:%d" % (k, (v.get("overlays") or v.get("keys") or 0)) for k, v in source_coverage.items()))
    except Exception as e:
        log.warning("SKIP overlay attach: %s", e); skips.append("overlay_attach")

    # 7b-snapshot. Daily overlay-fired snapshot (backtestable artifact). -----
    #     Writes docs/data/_overlay_history/<date>.json — compact list of every
    #     pick / opp leader that had overlays attached today, with close price.
    #     OVERLAY-NOT-SCORER: reads overlays + price only; never reads score/factors
    #     via this path; the golden-additive invariant is intact.
    #     Wrapped in try/except → SKIP-not-abort on any error.
    try:
        _opp_leaders = list((opp or {}).get("leaders", [])) if opp else []
        overlay_snapshot.write_snapshot(
            date_str, pick_cards, _opp_leaders, ranked=ranked)
    except Exception as e:
        log.warning("SKIP overlay_snapshot: %s", e); skips.append("overlay_snapshot")

    # 8. Deliver: local file (base) then email (additive) -------------------
    # dry_run=True: skip all side-effecting writes so the PR-gate can exercise the full
    # pipeline (including build_report) without touching disk, email, or the git tree.
    if dry_run:
        log.info("DRY-RUN: skipping notifier_file.write_report + notifier_email.send_email")
        path = None
        sent = False
    else:
        path = notifier_file.write_report(markdown, date_str)
        sent = notifier_email.send_email(f"📈 SmartStock 每日投資日報 {date_str}", markdown)

    # 8b. Web export for the PWA (history JSON + index) ----------------------
    if web and not dry_run:
        payload = web_export.build_payload(
            date_str, news, indices, inst, ranked, analyses,
            target, reb, risk, markdown, skips, movers=movers, level_map=level_map,
            delta=delta_changes, events=events, breadth=breadth, revenue=revenue_data,
            signals=sig, themes=themes, opportunity=opp, pick_cards=pick_cards,
            regime=regime, concentration=concentration, shortvol=shortvol_board,
            macro=macro_ctx, fx=fx, watchlist=wl_board, early_board=early_board,
            overlays_map=overlays_map, source_coverage=source_coverage,
            environment=environment, my_positions=my_positions,
            momentum_portfolio=momentum_lens, scored_universe=scored_universe,
            validated_portfolio=validated_lens, factor_validation=factor_validation,
            concentration_summary=conc_summary)
        # Audit finding 4: surface which scored names carry the partial-inputs cap
        # (板塊外資料不全 → 燈號封頂觀察). Compact summary: count + the ≤12 capped
        # board rows (scored_universe already carries partial_inputs/partial_reason
        # per row). The full per-name flag lives in _verdicts.json entries — panel/US
        # names merged after this export are covered there, not re-inlined here.
        _partial_syms = sorted(s for s, v in verdict_map.items()
                               if isinstance(v, dict) and v.get("partial"))
        payload["partial_inputs"] = {
            "reason": verdict_mod.PARTIAL_REASON,
            "count": len(_partial_syms),
            "board_symbols": [r.get("stock") for r in (scored_universe or [])
                              if r.get("partial_inputs")],
        }
        data_dir = web_export.export(payload, config.WEB_DIR)
        log.info("web data exported: %s", data_dir)

        # Fix 3 (GAP A): emit the all-market search index so the PWA can resolve ANY listed
        #     code/name, not just the ~30 actionable names. SEPARATE lightweight cached file —
        #     never inlined into the per-day payload. SKIP-not-abort (partial sources degrade).
        try:
            _uni_rows = universe_mod.full_market_index()
            if _uni_rows:
                _up = web_export.write_universe_index(_uni_rows, config.WEB_DIR)
                log.info("universe index: %d names → %s", len(_uni_rows), _up)
        except Exception as _ue:
            log.warning("SKIP universe index: %s", _ue); skips.append("universe_index")

        # #3 option B: accumulate the keyless whole-market OHLC panel (one-call TWSE+TPEx
        #     snapshot, no per-stock fetch / no 429) and score names with enough history into
        #     the verdict map — widening daily coverage from the ~600 opp universe toward full
        #     TW over time. Panel is gitignored + CI-cached (raw, reconstructable); only the
        #     scored OUTPUT (_verdicts.json) is served. SKIP-not-abort; cold-start ramps.
        try:
            _panel_path = os.path.join(config.WEB_DIR, "data", "_panel.json.gz")
            _panel = market_panel.load(_panel_path)
            _snap = universe_mod.market_ohlc_snapshot()
            if _snap:
                market_panel.append_snapshot(_panel, date_str, _snap)
                market_panel.save(_panel_path, _panel)
                _pf = market_panel.panel_frames(_panel, min_bars=config.MIN_BARS)
                if _pf:
                    _panel_ranked = strategy.rank_stocks(_pf, frames=frames)
                    # Audit finding 4: panel scoring has neither inst NOR chips (frames
                    # only) → partial-inputs cap; the entry itself carries partial+reason
                    # into _verdicts.json (this block runs AFTER the payload export, so
                    # the per-entry flag is the durable marker for these names).
                    for _pr in _panel_ranked:
                        verdict_map.setdefault(
                            _pr["stock"],
                            verdict_mod.capped_entry(_pr["score"],
                                                     _pr.get("inputs_complete", False)))
                    # Chart coverage: panel-scored TW names are clickable in all-market search
                    #   (verdict badge) but had NO detail file → no 技術線圖. Write one per panel
                    #   frame (reconstructed OHLC already in hand). Own export (main details flushed
                    #   at the overlay-attach step). SKIP-not-abort.
                    try:
                        _pnames = (opp or {}).get("names") or {}
                        _pdetails = {}
                        for _pcode, _pdf in _pf.items():
                            if _pdf is None or getattr(_pdf, "empty", True):
                                continue
                            _pdetails[_pcode] = stock_detail.build_detail(
                                _pcode, df=_pdf,
                                name=_pnames.get(_pcode) or config.STOCK_NAMES.get(_pcode))
                        if _pdetails:
                            stock_detail.export_details(_pdetails, config.WEB_DIR)
                            log.info("panel chart detail files: +%d", len(_pdetails))
                    except Exception as _pde:
                        log.warning("SKIP panel detail files: %s", _pde)
                    log.info("market panel: %d names accumulated, %d scored into verdicts",
                             len(_panel), len(_panel_ranked))
        except Exception as _pe:
            log.warning("SKIP market panel: %s", _pe); skips.append("market_panel")

        # #3: emit the verdict map so all-market search shows 買入/觀望/不持有 for every scored
        #     name (core + opportunity universe + panel). SKIP-not-abort.
        try:
            _vp = web_export.write_verdicts_index(verdict_map, config.WEB_DIR)
            log.info("verdicts index: %d scored names → %s", len(verdict_map), _vp)
        except Exception as _ve:
            log.warning("SKIP verdicts index: %s", _ve); skips.append("verdicts_index")

        # 8c. W1 pick-outcome backfill — "did our picks actually work?". Runs AFTER the
        #     payload is written (so today's <date>.json is on disk and globbable). For the
        #     ~10 most-recent trading-day pick files, replay the post-pick prices and write
        #     docs/data/_outcomes/<date>.json (IDEMPOTENT — a complete file is skipped, no
        #     refetch). Then attach a rolling hit-rate onto the payload and re-export.
        #     OVERLAY-NOT-SCORER: pick_performance is INFORMATIONAL self-evaluation only —
        #     it NEVER feeds strategy.score_stock / rank_stocks. SKIP-not-abort throughout.
        try:
            _pick_files = sorted(
                f for f in glob.glob(os.path.join(data_dir, "*.json"))
                if not os.path.basename(f).startswith("_")
                and os.path.basename(f) != "index.json"
            )
            for _fp in _pick_files[-10:]:                 # ~10 recent trading days
                _asof = os.path.basename(_fp)[:-5]        # strip '.json'
                try:
                    pick_outcomes.compute_outcomes(data_dir, _asof, n_days=5)
                except Exception as _oe:
                    log.warning("SKIP compute_outcomes %s: %s", _asof, _oe)
            payload["pick_performance"] = pick_outcomes.summarize_hit_rate(data_dir)
            # Fix 5: D+20 horizon — a SEPARATE ledger (n_days=20, _outcomes_20 subdir) so the
            #     D+5 stop/idempotency path above stays byte-identical. Merged additively into
            #     pick_performance as d20_win_rate / avg_ret_20 / n_scored_20. SKIP-not-abort.
            try:
                for _fp20 in _pick_files[-10:]:
                    _asof20 = os.path.basename(_fp20)[:-5]
                    try:
                        pick_outcomes.compute_outcomes(data_dir, _asof20, n_days=20,
                                                       out_subdir="_outcomes_20")
                    except Exception as _oe20:
                        log.warning("SKIP compute_outcomes(D+20) %s: %s", _asof20, _oe20)
                _d20 = pick_outcomes.summarize_horizon(data_dir, "_outcomes_20", "ret_20")
                payload["pick_performance"]["d20_win_rate"] = _d20["win_rate"]
                payload["pick_performance"]["avg_ret_20"] = _d20["avg_ret"]
                payload["pick_performance"]["n_scored_20"] = _d20["n_scored"]
            except Exception as _e20:
                log.warning("SKIP D+20 pass: %s", _e20)
            # 8c-radar. Fix 2 (GAP E): radar forward-accuracy ledger — track the opportunity-
            #     leader + scored_universe cohort the SAME way picks are tracked, in a SEPARATE
            #     _radar_outcomes ledger (custom picks_loader). Surfaced as radar_performance so
            #     "雷達準確率" is answerable. OVERLAY-NOT-SCORER: never feeds scoring. SKIP-not-abort.
            try:
                for _fpr in _pick_files[-10:]:
                    _asofr = os.path.basename(_fpr)[:-5]
                    try:
                        pick_outcomes.compute_outcomes(
                            data_dir, _asofr, n_days=5,
                            out_subdir=radar_outcomes.RADAR_SUBDIR,
                            picks_loader=radar_outcomes.load_leaders)
                    except Exception as _oer:
                        log.warning("SKIP radar outcomes %s: %s", _asofr, _oer)
                payload["radar_performance"] = radar_outcomes.summarize_radar(data_dir)
                _rp = payload["radar_performance"]
                log.info("radar performance: %d scored over %d dates, win %.0f%%",
                         _rp.get("n_scored") or 0, _rp.get("n_dates") or 0,
                         100 * (_rp.get("win_rate") or 0.0))
            except Exception as _er:
                log.warning("SKIP radar performance: %s", _er); skips.append("radar_performance")
            # 8c-attr. Performance attribution (P2-S1): JOIN the freshly-written _outcomes rows
            #     back to their same-day picks → which TRIGGERED signals / regimes our picks
            #     actually rode, plus a HYPOTHETICAL equal-weight NAV replay. Runs AFTER
            #     compute_outcomes so _outcomes/<date>.json is on disk for attribution to read.
            #     OVERLAY-NOT-SCORER: pure read of local JSON, INFORMATIONAL self-evaluation —
            #     NEVER summed into strategy.score_stock / rank_stocks. SKIP-not-abort.
            try:
                payload["attribution"] = attribution_mod.summarize(data_dir)
                _at = payload["attribution"]
                log.info("attribution: %d scored, %d signal(s), NAV %.2f%% (accruing=%s)",
                         _at.get("n_scored") or 0, len(_at.get("by_signal") or {}),
                         (_at.get("nav") or {}).get("total_ret") or 0.0, _at.get("accruing"))
            except Exception as _ae:
                log.warning("SKIP attribution: %s", _ae); skips.append("attribution")
            # re-export so the freshly-computed hit-rate + attribution land in today's <date>.json.
            web_export.export(payload, config.WEB_DIR)
            _pp = payload["pick_performance"]
            log.info("pick performance: %d scored / %d picks over %d dates",
                     _pp.get("n_scored") or 0, _pp.get("n_picks") or 0, _pp.get("n_dates") or 0)
        except Exception as e:
            log.warning("SKIP pick_outcomes: %s", e); skips.append("pick_outcomes")

        # 8d. Premortem countermeasures (P-M1/2/3): strategy death-criteria,
        #     shadow portfolio (strategy curve vs execution curve), data-health
        #     gate. All three are INFORMATIONAL payload overlays — OVERLAY-NOT-
        #     SCORER: they NEVER feed strategy.score_stock / rank_stocks.
        #     SKIP-not-abort for P-M1/2; P-M3 (health) is FAIL-OPEN: it is
        #     ALWAYS computed and ALWAYS shipped — a crash inside the health
        #     check degrades the block, never blocks the daily report.
        try:
            # why: P-M1 — a dead signal must demote itself; the PWA banner
            # reads strategy_health, the weights stay human+CI-gated.
            payload["strategy_health"] = strategy_health_mod.summarize(data_dir)
        except Exception as e:
            log.warning("SKIP strategy_health: %s", e); skips.append("strategy_health")
        try:
            # why: P-M2 — chain today's top-N picks into the shadow NAV so the
            # strategy curve is separable from any execution drift.
            payload["shadow"] = shadow_mod.update(data_dir, date_str)
        except Exception as e:
            log.warning("SKIP shadow_portfolio: %s", e); skips.append("shadow_portfolio")
        try:
            # why: P-M3 — health must always ship (fail-open), so silent data
            # rot is bannered instead of rendered as a fresh report.
            payload["health"] = data_health_mod.summarize(payload, data_dir)
        except Exception as e:
            log.warning("data_health failed (fail-open → degraded): %s", e)
            payload["health"] = {"generated_at": payload.get("generated_at"),
                                 "sources": [], "overall": "degraded",
                                 "note": f"health check crashed: {e}"}
        try:
            web_export.export(payload, config.WEB_DIR)   # land 8d keys in <date>.json
            _h = payload.get("health") or {}
            log.info("health: %s (%d checks) | strategy_health: %d signal(s) | "
                     "shadow NAV %.4f (%d open)",
                     _h.get("overall"), len(_h.get("sources") or []),
                     len((payload.get("strategy_health") or {}).get("signals") or {}),
                     (payload.get("shadow") or {}).get("nav") or 1.0,
                     (payload.get("shadow") or {}).get("n_open") or 0)
        except Exception as e:
            log.warning("SKIP 8d re-export: %s", e); skips.append("health_export")

    if skips:
        log.warning("DONE — 部分來源略過: %s", ", ".join(sorted(set(skips))))
    else:
        log.info("DONE — 全部來源正常")
    log.info("report: %s | email_sent: %s", path, sent)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SmartStock daily run")
    parser.add_argument("--web", action="store_true",
                        help="also export PWA JSON to web/data/")
    parser.add_argument("--dry-run", action="store_true",
                        help="run full pipeline (including build_report) but skip all "
                             "disk writes, email, and Sheet sync — used by the PR-gate "
                             "CI job to catch kwarg/schema drift before merge")
    parser.add_argument("--date", dest="date_arg", default=None, metavar="YYYY-MM-DD",
                        help="override run date (default: today UTC); used for backfill. "
                             "Example: --date 2026-06-22")
    args = parser.parse_args()
    main(web=args.web, dry_run=args.dry_run, date_arg=args.date_arg)
