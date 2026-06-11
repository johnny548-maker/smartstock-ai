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
import watchlist_tracker
import stock_detail
import overlay_snapshot
import pick_outcomes

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


def main(web=False):
    setup_logging()
    log = logging.getLogger("main")
    date_str = datetime.now().strftime("%Y-%m-%d")
    skips = []
    log.info("=== SmartStock daily run %s ===", date_str)

    # 1. Market context + signal --------------------------------------------
    frames, indices = {}, {}
    try:
        frames, indices = data_fetcher.get_market_context()
    except Exception as e:
        log.warning("SKIP market context: %s", e); skips.append("indices")
    try:
        signal = data_fetcher.build_market_signal(frames, indices)
    except Exception as e:
        log.warning("SKIP market signal: %s", e); signal = {"risk": "LOW"}; skips.append("signal")
    risk = signal.get("risk", "LOW")

    # 2. News ----------------------------------------------------------------
    try:
        news = news_digest.get_news()
    except Exception as e:
        log.warning("SKIP news: %s", e); news = {}; skips.append("news")

    # 2b. Market breadth (broad basket → 參與度) ----------------------------
    try:
        breadth = breadth_mod.get_breadth()
    except Exception as e:
        log.warning("SKIP breadth: %s", e); breadth = None; skips.append("breadth")
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
    try:
        revenue_data = revenue_mod.get_early_candidates()
    except Exception as e:
        log.warning("SKIP revenue: %s", e); revenue_data = None; skips.append("revenue")

    # 2d. 主題湧現 (news-driven theme rotation → 供應鏈 tickers) --------------
    try:
        themes = theme_mod.get_themes(news)
    except Exception as e:
        log.warning("SKIP themes: %s", e); themes = []; skips.append("themes")

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

    # 3. 三大法人 ------------------------------------------------------------
    try:
        inst = institutional.get_institutional(config.STOCKS_TW)
    except Exception as e:
        log.warning("SKIP institutional: %s", e); inst = {}; skips.append("institutional")
    if not inst:
        skips.append("institutional")

    # 4. Stock data + ranking ------------------------------------------------
    all_syms = config.STOCKS_TW + config.STOCKS_US
    try:
        data = data_fetcher.get_stock_data(all_syms)
    except Exception as e:
        log.warning("SKIP stock data: %s", e); data = {}; skips.append("stock_data")
    # 4a. 籌碼 cross-run buffer: record today (trading days only), derive chips
    chips_state = chip_state.load()
    # 4a-fund. Fundamentals overlay cache + revenue state (no network; B12). The
    #     revenue STATE (full YoY buffer) is loaded separately from revenue_data
    #     (today's candidate list) because build_badge needs the per-code YoY history.
    fund_cache = fundamentals.load_cache()
    try:
        rev_state = revenue_mod.load_state()
    except Exception as e:
        log.warning("SKIP revenue state load: %s", e); rev_state = None; skips.append("fundamentals")
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
    except Exception as e:
        log.warning("SKIP concentration: %s", e); concentration = None

    # 5d. Earnings-blackout overlay (analyst G5): flag picks with a binary earnings
    #     event in the next 7d — INFORMATIONAL only, never a score change.
    try:
        ecache = os.path.join(config.WEB_DIR, "data", "_earnings_cache.json")
        earnings_bo = earnings_mod.annotate(
            [it["stock"] for it in ranked[:config.DISPLAY_N]], cache_path=ecache)
        for sym, b in earnings_bo.items():
            if sym in pick_cards:
                pick_cards[sym]["earnings"] = b
        if earnings_bo:
            log.info("earnings blackout: %d pick(s) within %dd", len(earnings_bo),
                     earnings_mod.WITHIN_DAYS)
    except Exception as e:
        log.warning("SKIP earnings guard: %s", e)

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
        concentration=concentration, macro=macro_ctx)

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
                    ticker, df=df_opp, name=ld.get("name"))
            for bc in (opp or {}).get("breakout", []):
                ticker = bc.get("stock") or bc.get("ticker")
                if not ticker or ticker in details:
                    continue
                df_opp = opp_data_ref.get(ticker)
                details[ticker] = stock_detail.build_detail(
                    ticker, df=df_opp, name=bc.get("name"))

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
    _pick_syms = [it["stock"] for it in ranked[:config.DISPLAY_N]]
    _tw_codes = {s.replace(".TWO", "").replace(".TW", "") for s in _pick_syms
                 if s.endswith((".TW", ".TWO"))}
    _us_syms = {s for s in _pick_syms if not s.endswith((".TW", ".TWO"))}

    # --- TWSE 上市 chip/法人/基本面 (T86 三大法人 + MI_MARGN 融資融券 + BWIBBU PE) -----
    try:
        t86_rows = _twse.fetch_t86()
        # T86 daily snapshot → archive for net_buy_streak history (one file per trading day)
        if t86_rows:
            try:
                parsed = [r for r in (_twse.parse_t86_row(x) for x in t86_rows) if r]
                _twse_archive = os.path.join(config.WEB_DIR, "data", "_t86_archive")
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
            try:
                _tdcc.save_weekly(tdcc_rows, _tdcc_date,
                                  archive_dir=os.path.join(config.WEB_DIR, "data", "_tdcc_archive"))
            except Exception as e:
                log.warning("SKIP tdcc weekly archive: %s", e)
            # last week's rows (if accrued) → WoW 大戶吸籌/散戶化 verdict, else snapshot-only.
            _tdcc_hist = _tdcc.load_history(
                archive_dir=os.path.join(config.WEB_DIR, "data", "_tdcc_archive"))
            _prior = sorted(k for k in _tdcc_hist if k != _tdcc_date)
            last_week_rows = _tdcc_hist.get(_prior[-1]) if _prior else None
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
            for code in list(details.keys()):
                ovs = _overlays_for(code)
                if ovs:
                    details[code] = _overlay.attach(details[code], ovs)
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
    path = notifier_file.write_report(markdown, date_str)
    sent = notifier_email.send_email(f"📈 SmartStock 每日投資日報 {date_str}", markdown)

    # 8b. Web export for the PWA (history JSON + index) ----------------------
    if web:
        payload = web_export.build_payload(
            date_str, news, indices, inst, ranked, analyses,
            target, reb, risk, markdown, skips, movers=movers, level_map=level_map,
            delta=delta_changes, events=events, breadth=breadth, revenue=revenue_data,
            signals=sig, themes=themes, opportunity=opp, pick_cards=pick_cards,
            regime=regime, concentration=concentration, shortvol=shortvol_board,
            macro=macro_ctx, fx=fx, watchlist=wl_board, early_board=early_board,
            overlays_map=overlays_map, source_coverage=source_coverage,
            environment=environment)
        data_dir = web_export.export(payload, config.WEB_DIR)
        log.info("web data exported: %s", data_dir)

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
            # re-export so the freshly-computed hit-rate lands in today's <date>.json.
            web_export.export(payload, config.WEB_DIR)
            _pp = payload["pick_performance"]
            log.info("pick performance: %d scored / %d picks over %d dates",
                     _pp.get("n_scored") or 0, _pp.get("n_picks") or 0, _pp.get("n_dates") or 0)
        except Exception as e:
            log.warning("SKIP pick_outcomes: %s", e); skips.append("pick_outcomes")

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
    args = parser.parse_args()
    main(web=args.web)
