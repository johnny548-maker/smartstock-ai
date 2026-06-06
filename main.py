# -*- coding: utf-8 -*-
"""SmartStock Daily AI System — orchestrator.

Pipeline: market data + news + 法人籌碼 → 選股打分 → 規則點評
          → 風險引擎 + 資產配置 + 再平衡 → 報告檔 + Email。

Every external stage is wrapped: a failure is logged as SKIP and the run
continues with whatever data is available (never a silent drop)."""
import argparse
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

# sources/ overlay framework (keyless informational overlays — OVERLAY-NOT-SCORER).
# Each fetcher is injectable + graceful-skip; the wiring below guards every source
# independently so a dead source SKIPs without aborting the run, and NOTHING here
# ever feeds strategy.rank_stocks / score_stock (golden-additive invariant).
from sources import overlay as _overlay
from sources import twse as _twse
from sources import tpex as _tpex
from sources import tdcc as _tdcc
from sources import sec as _sec


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
        watchlist_tracker.enroll(wl, ranked[:config.DISPLAY_N], pins=[], date=date_str)
        watchlist_tracker.reevaluate(wl, data, frames, date_str)
        watchlist_tracker.save(wl, wl_path)
        wl_board = watchlist_tracker.board(wl)
        log.info("watchlist: %d tracked name(s) on board", len(wl_board))
    except Exception as e:
        log.warning("SKIP watchlist tracker: %s", e); skips.append("watchlist")

    # 7c. Per-stock detail files (REQ1 long-tail) — standalone JSON so revenue candidates
    #     and other displayed names open a usable detail view in the PWA WITHOUT a new
    #     network fetch in the cron hot path. Revenue candidates have NO OHLCV in hand →
    #     build_detail(df=None,...) is metadata-only. SKIP-not-abort.
    try:
        details = {}
        # Revenue candidates: metadata-only (no df, no new fetch), name + fundamental badge.
        for c in (revenue_data or {}).get("candidates", []):
            code = c.get("code")
            if not code:
                continue
            try:
                badge = fundamentals.build_badge(
                    code, rev_state=rev_state, fund_cache=fund_cache, is_tw=True)
            except Exception:
                badge = None
            details[code] = stock_detail.build_detail(
                code, df=None, name=c.get("name"), fundamental=badge)
        # Picks: a standalone file each (df + levels in hand) so a deep-link always resolves.
        for item in ranked[:config.DISPLAY_N]:
            sym = item["stock"]
            if sym in details:
                continue
            details[sym] = stock_detail.build_detail(
                sym, df=data.get(sym), name=config.STOCK_NAMES.get(sym),
                fundamental=(pick_cards.get(sym) or {}).get("fundamental"),
                levels=level_map.get(sym))
        if details:
            written = stock_detail.export_details(details, config.WEB_DIR)
            log.info("detail files: %d written", len(written))
    except Exception as e:
        log.warning("SKIP detail files: %s", e); skips.append("detail_files")

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
                     ", ".join("%s:%d" % (k, v["overlays"]) for k, v in source_coverage.items()))
    except Exception as e:
        log.warning("SKIP overlay attach: %s", e); skips.append("overlay_attach")

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
            overlays_map=overlays_map, source_coverage=source_coverage)
        data_dir = web_export.export(payload, config.WEB_DIR)
        log.info("web data exported: %s", data_dir)

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
