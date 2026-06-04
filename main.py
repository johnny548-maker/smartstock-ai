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
        signals=sig, themes=themes)

    # 8. Deliver: local file (base) then email (additive) -------------------
    path = notifier_file.write_report(markdown, date_str)
    sent = notifier_email.send_email(f"📈 SmartStock 每日投資日報 {date_str}", markdown)

    # 8b. Web export for the PWA (history JSON + index) ----------------------
    if web:
        payload = web_export.build_payload(
            date_str, news, indices, inst, ranked, analyses,
            target, reb, risk, markdown, skips, movers=movers, level_map=level_map,
            delta=delta_changes, events=events, breadth=breadth, revenue=revenue_data,
            signals=sig, themes=themes)
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
