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

## Confirmed Issues（待修，按嚴重度）
（稽核後填入）

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
