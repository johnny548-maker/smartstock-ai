# -*- coding: utf-8 -*-
"""SmartStock Daily AI System — orchestrator.

Pipeline: market data + news + 法人籌碼 → 選股打分 → 規則點評
          → 風險引擎 + 資產配置 + 再平衡 → 報告檔 + Email。

Every external stage is wrapped: a failure is logged as SKIP and the run
continues with whatever data is available (never a silent drop)."""
import argparse
import json
import logging
import os
from datetime import datetime

import config
import data_fetcher
import news_digest
import institutional
import strategy
import ai_analyzer
import asset_allocation
import rebalance
import report_builder
import notifier_file
import notifier_email
import web_export


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
    ranked = strategy.rank_stocks(data, institutional_map=inst)
    log.info("ranked %d / %d symbols", len(ranked), len(all_syms))

    # 5. Rule-based commentary for the Top N --------------------------------
    analyses = {}
    for item in ranked[:config.TOP_N]:
        analyses[item["stock"]] = ai_analyzer.analyze_stock(
            item["stock"], item["score"], item["factors"], item.get("sector"))

    # 6. Allocation + rebalance ---------------------------------------------
    base = asset_allocation.base_allocation()
    target = asset_allocation.adjust_allocation(base, signal)
    current = load_portfolio_state()
    reb = rebalance.rebalance(current, target) if current else {}

    # 7. Build the report ----------------------------------------------------
    markdown = report_builder.build_report(
        date_str=date_str, news=news, indices=indices, institutional=inst,
        ranked=ranked, analyses=analyses, allocation=target,
        rebalance_diff=reb, risk=risk)

    # 8. Deliver: local file (base) then email (additive) -------------------
    path = notifier_file.write_report(markdown, date_str)
    sent = notifier_email.send_email(f"📈 SmartStock 每日投資日報 {date_str}", markdown)

    # 8b. Web export for the PWA (history JSON + index) ----------------------
    if web:
        payload = web_export.build_payload(
            date_str, news, indices, inst, ranked, analyses,
            target, reb, risk, markdown, skips)
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
