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

### 3c MED（d79e3bb）+ 瀏覽器驗
- [x] #13 bare-code verdict badge → _verdictOf 試 +.TW/+.TWO（驗 9999→建議買入、8888.TWO→不持有）
- [x] #16 opp/US detail 立即 flush（防 late-export exception 全失）；#12 cMOM clamp 統一 VOLTGT_FLOOR=0.5

## Wave 4（第三波稽核 wkph4k5qy：8 agents, 4 HIGH）
### 4a CRITICAL + robustness（7e76f3c）
- [x] **CRITICAL `_bench_for` DataFrame crash**：我 Wave3a 寫 `frames.get("nasdaq") or ...` → `DataFrame or X` raise「ambiguous」→ rank_stocks try/except 靜默**丟掉所有美股**（06-21 picks 0 美股）。我的單元測試用 string 沒抓到。修=explicit `is not None` + bool-raising 回歸測試。**驗：picks US 4(TSM/AMD/QQQ/GOOGL)、verdicts US 791 恢復**
- [x] #3 incomplete：us_market.score_batch 只收 sp500 → 整個輪轉美股仍比 S&P。加 nasdaq_frame
- [x] revenue.parse_rows isinstance guard；_clean numpy scalar(.item())；_rebuild_index log+.get()；detail bare 去重；ResizeObserver 洩漏；OOS relabel 漏網
### 4b HIGH iOS 返回手勢（76888ed）
- [x] openStockSheet pushState（可 pop）+ route() code-less hash 關閉 sheet。驗：open→back→關閉+留 app
### 4c HIGH 無障礙（c1e611e）
- [x] openSheet inert 背景+focus 入 sheet；close un-inert+還原。驗 0000→1111→0000
### CI race（bc2a16e）
- [x] daily.yml report+index push 改 rebase-retry×5（CI 27889451150 曾因 push race 丟整份報告）
### 4d LOW polish（389fd49）
- [x] chgHtml toFixed(2)；revenue code-less 列改 static 非 dead <a>

## 待辦（Deferred — 低值/風險/數據限制）
- SW network-first 全部（MED，**SW 改動風險高**，PWA 白屏前科 → 暫不動，網路慢非用戶抱怨）
- detail 檔不 prune（MED，與「完整」衝突 → 用戶選完整，接受）；24 revenue bare 無 badge（MED，cold-start ramp benign）
- 核心 fetcher 無 last-good cache（LOW resilience）；universe/institutional schema KeyError（LOW）；date_str UTC（LOW latent）
- #8 .TWO vs 櫃買（無 keyless TPEx index，TWSE 是可辯護 proxy）；#11 delisting=price-freeze（與 #1 同數據限制）

## 待辦（第二波 MED/LOW — 已於 Wave3c/4 處理）
- #1 完整 PIT：**數據限制，不做 masking 重跑**。added_date 86%(563/653)回填到 2011(窗起點，非真 index-add)，CSV=2026 snapshot 無下市名 → mask near-no-op（bulk 本就從 2011 可選）且救不了真 survivorship（缺失名不在 CSV）。誠實標註（已部署 1f9ee78）= operative fix。完整 PIT 須歷史成分數據集（含下市名）= data-acquisition 任務非 code。
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
