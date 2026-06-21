# SmartStock 持續稽核+優化 — 跨迭代狀態檔

> 自主迴圈（用戶核准 2026-06-21 03:20，跑到 12pm）。每波次：稽核→驗證→修→測→commit→push（自動部署）。
> 停止條件：時間 ≥ 12:00 或 confirmed-issue 清單清空（連續 2 波無新確認問題）。

## 部署狀態
- PR #7 merged → main（全 session 修正 + 線圖覆蓋）2026-06-21 03:2x
- CI dispatch run 27881383905（重產資料 + 全 detail 檔）
- APP_VERSION v45

## 波次日誌
### Wave 1 — 03:2x（部署 + 首稽核）
- 部署完成；啟動 6-inspector 稽核 workflow

## Wave 2 已修（commit）
- [x] #5 dead 429 retry 復活（get_universe raise_on_empty）+ 4測 — 99c8d6b
- [x] #1 light self-name；names 已確認 12/12（新 CI payload）— 99c8d6b
- [x] #263 sleep tautology — 99c8d6b
- [x] #11 US store 過期（21d）+測 — bf06bef
- [x] #13 movers 可點（findCard）— bf06bef
- [x] #15 payload picks ohlc 剝除（365→318KB，detail-fetch 還原；瀏覽器驗 3008.TW）— a47c463
- [x] #20 TW _get bounded 重試 — f98360d
- [x] #7 opp/US 覆蓋進 source_coverage（collapse→degraded）— f98360d
- 部署：PR#7 merged + 3 CI dispatch；SLAB+冷門股圖上線（763 detail 檔，瀏覽器驗）。

## Confirmed Issues（待修，按嚴重度）
- #9 SW network-first re-download 452KB（perf，但 SW 改動風險高，謹慎）
- #12 theme toggle 不重染 sparkline（UX niche）
- #15b 可續剝 opportunity ohlc（再省 ~93KB）
- #17 detail-file git churn（用戶選完整 → 暫接受，記 tradeoff）
- #21 cached_fetch stale 無信號；#231 dead NameError guard（LOW cleanup）
- 第二波稽核完成（13 agents, 22 raw, 8 HIGH confirmed）→ Wave 3 修

## Wave 3 已修（第二波稽核）
### 3a 評分正確性（20e67bd）
- [x] #2 我的 #7 是 no-op（只改 main 沒改 data_health）→ data_health 讓 opp/us collapse=degraded +測
- [x] #3 NASDAQ 誤比 S&P500（^IXIC 抓了沒用）→ US 改 nasdaq frame
- [x] #4 52週高 20-bar 就 fire（mislabel）→ gate ≥0.8*252 bars
- [x] #9 .replace('.TW') 把 .TWO 改成 '8069O' → _bare() helper
- [x] #10 verdict_line 用 insertion-order 非 dominant → sort by magnitude
- [x] #17 int 截斷 half-weight inst buy → round()
### 3b 回測誠實標註（1f9ee78）
- [x] #1 CRITICAL survivorship + 成分 look-ahead → docstring+DISCLAIMERS 點名兩偏誤+標 upper-bound
- [x] #5 OOS 是 in-sample 尾段 → relabel（含 PWA card「近2年尾段·非獨立」）
- [x] #6 lockbox 洩漏（champion+DSR/PBO 含 lockbox）→ render CAVEAT 誠實化
- [x] #7 docstring 過時 top-20/12-1 → 同步 top-10/6-1；#18 WilsonLo 0.51-0.54

## 待辦（第二波 MED/LOW + CRITICAL 完整修）
- #1 完整 PIT：plumb added_date → run_sleeve/run_grid mask added_date<=sig → 重跑 TW/US（數字會降）
- #8 .TWO 仍比 TWSE 非櫃買（無 TPEx index）；#11 delisting=price-freeze；#12 cMOM clamp 不一致
- #13 d.search 15 bare TW code → verdict badge miss；#14 26 bare detail 重複檔
- #16 opp/US detail 只在 late export flush（exception 全失）；#19 _us_verdicts 冗餘；#20 QQQ 無 universe

## Fixed（已修，附 commit）
### Wave 1（03:2x–resume）
- [x] **#2 touch-scroll**（HIGH，你「難往下滑」真兇）— `.sheet{touch-action:none}` 擋 body 捲；none 移到 grip + body 加 pan-y。style.css
- [x] **#3 TPEx benchmark**（HIGH，scorer 正確性）— `_bench_for` `.TWO` 誤比 S&P500；改 `endswith((".TW",".TWO"))`。strategy.py + 測試
- [x] **#4 panel detail**（HIGH，361 TW 名無圖）— panel block 加 build_detail loop + 自身 export。main.py
- [x] **#6 nasdaq 凍結快取**（HIGH）— now_ts=0 → TTL 永不過期；default time.time()。nasdaq_trader.py
- [x] **#8/#256 chart placeholder + bare-code fallback**（MED）— 無圖顯「輪批中」note；detail fetch 試替代 code form。app.js
- [x] **#22 weekend bday lag**（LOW，假 degraded）— ref 錨到最後交易日。data_health.py + 測試
- [x] **#224 us_market NaN**（LOW）— dropna(subset=Close) 防 trailing NaN 污染 price。us_market.py
- 320 targeted tests 綠；golden 不破；v46。

## Won't-fix / Deferred
（記錄不修的 + 理由）
