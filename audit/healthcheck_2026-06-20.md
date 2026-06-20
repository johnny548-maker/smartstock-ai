# SmartStock AI — 全方位健檢報告

**日期**：2026-06-20 ｜ **分支**：`feat/healthcheck-allmarket-20260620` ｜ **方法**：3 Explore + Plan agent 摸清架構 → 親驗 file:line → TDD 補洞 → 全 1610 test 綠

> 數字來源標註：`[實測]`=本次跑/讀檔；`[檔]`=既有 backtest 輸出（最後跑 2026-06-12，因 `ohlcv_15y/` cache 已清空，未重跑 ~350min 全回測）。

---

## 0. 總評

整體**健康、嚴謹**。回測有 walk-forward + OOS + Wilson-CI + DSR/PBO/CSCV，且**回測結果真的回授到推薦權重**。本次健檢找到 4 個真缺口（搜尋涵蓋、每日評分涵蓋、雷達準確率追蹤、overlay 涵蓋）並全數補洞；另 3 個用戶疑慮經親驗為**已正常或高估**（picks 排序、資料源接入、15y 數字上 UI）。

---

## 1. 用戶 10 問逐項回答

| # | 用戶問題 | 結論 | 證據 / 處置 |
|---|---|---|---|
| 1 | 股票推薦獲利率 | **有，且本次補強**。picks D+5 勝率 **45.1%**、避停損率 57.1%（n=51 / 11 日）`[實測]`；新增 **D+20** 長線 horizon（Fix 5） | `pick_outcomes.summarize_hit_rate`；payload `pick_performance` |
| 2 | 雷達找尋準確率 | **原本缺 → 已補（Fix 2）**。雷達當「推薦群」原無前向追蹤，新增 `radar_performance` ledger | `radar_outcomes.py`；payload `radar_performance` |
| 3 | 頁面是否直覺 | **良好**。scroll-snap deck、ARIA、empty/staleness state、色盲友善燈號；小缺：icon 按鈕辨識度、無 spinner（可接受） | `docs/app.js`、`style.css` |
| 4 | 資料抓取是否完整 | **12 源全接（keyless）**；原 overlay 只貼 ~18 檔 picks → **已擴涵蓋（Fix 4）** 到雷達+全市場精選群 | `main.py` overlay scope |
| 5 | 15y 回測準確率 | **真實**：2012-07→2026-06 walk-forward。momentum **CAGR 36.48% / Sharpe 1.42 / MaxDD −40.7% / NAV 76.2× / OOS 2y 72.41%** `[檔]` | `backtest_portfolio_tw.txt` |
| 6 | 是否依回測調整推薦 | **有**。backtest verdict → `config.LEAD_*` → strategy.py live read；2026-06-13 demote 5 訊號（首次新高 lift 2.44→0.68） | `strategy.py:195`、`config.py:199-206`、`.decisions/2026-06-13` |
| 7 | 雷達是否照推薦順序排 | **原按(來源數,ready,RS) → 已改按推薦分（Fix 6）**。picks 本就按 score 排（無問題） | `app.js radarMerge`、`strategy.py:238` |
| 8 | 是否含代號+名稱 | **有**（picks/雷達/搜尋每列「名稱 + 代碼」並陳） | `app.js:559`、`web_export._names_map` |
| 9 | 搜尋是否含所有股票 | **原只 ~30 檔 → 已補全市場（Fix 3）**。新出 `_universe.json`（全 TWSE+TPEx+US ~1800 檔） | `universe.full_market_index`、`web_export.write_universe_index` |
| 10 | 回測是否用所有股票 | **回測用 661**；每日**原只評分 28 核心 → 已補全市場評分（Fix 1）**：~600 機會池過同一 gated 公式 | `universe_15y_draft.csv`；`main.py` scored_universe |

---

## 2. 實測數字（profit rate / accuracy）

### 2a. 15y 組合回測 `backtest_portfolio_tw.txt`（2012-07-02 → 2026-06-12，net-of-cost：滑價15+手續30+賣稅30 bps）
| 策略 | CAGR | Sharpe | MaxDD | finalNAV | 月勝率>基準 | WilsonLo |
|---|---|---|---|---|---|---|
| **momentum（採用）** | **36.48%** | **1.42** | −40.7% | 76.17× | 96/167 季 | 0.499 |
| equal_weight | 23.65% | 1.46 | −30.0% | 19.25× | 97/167 | 0.505 |
| buy_hold 0050 | 20.18% | 1.13 | −33.8% | 12.96× | — | — |

**OOS 末 2 年（2024-06→2026-06）**：momentum CAGR +72.41% / Sharpe 1.77。

### 2b. 每日 picks 前向準確率 `[實測]`
- D+5 勝率 **45.1%**、避停損率 57.1%、n_scored=51、n_dates=11（payload `pick_performance`）。
- 本次新增 D+20 horizon + 雷達 `radar_performance`（首次跑後開始累積樣本）。

### 2c. 訊號 gate `backtest_15y_hardened.txt`（661-univ、Wilson-CI + Bonferroni + BH）
- **保留**：U/D量吸籌 lift 1.55、VDU→Thrust lift 1.61。
- **淘汰**：首次新高 0.68、Power pivot 1.24、Stage2 1.00、Pocket pivot 0.99、RS線新高 0.99（全 demote 至權重 0）。

---

## 3. 7 項補洞狀態

| Fix | 缺口 | 狀態 | 作法（皆不動 scorer 公式 → 不觸發 golden gate） |
|---|---|---|---|
| **1** | C 每日只評分 28 核心 | ✅ **已修** | 對 ~600 機會池 `_data` 再呼叫同一 `rank_stocks`，產 `scored_universe`（price 加值），併入雷達板 |
| **2** | E 雷達無準確率追蹤 | ✅ **已修** | `radar_outcomes.py` 鏡射 pick 引擎，獨立 `_radar_outcomes` ledger → `radar_performance` |
| **3** | A 搜尋只 ~30 檔 | ✅ **已修** | `_universe.json` 全市場索引 + app.js 雙群組搜尋（當日精選 + 全市場 fallback） |
| **4** | D overlay 只貼 18 檔 | ✅ **已修** | overlay symbols set 擴含 leaders + scored_universe（資料本就全市場抓） |
| **5** | D+20 長線報酬缺 | ✅ **已修** | 獨立 `_outcomes_20` pass（n_days=20），D+5 stop/idempotency 路徑 byte-identical |
| **6** | B 雷達非照推薦分排 | ✅ **已修** | radarMerge 併入統一 score 為主排序鍵（照推薦順序） |
| **7** | 15y 數字未上 UI | ✅ **已存在（親驗）** | `momentumHtml`→`momTrackCards` 早已渲染 15y CAGR/Sharpe/MaxDD/OOS/vs-bench（動能組合 tab）；無需改 code |

> **誠實校正**：初判 GAP B/D 為大缺口，親驗後發現 picks 已照 score 排（B 僅雷達）、12 源全接（D 僅 overlay 涵蓋窄）；Fix 7 經查 15y 數字早在 UI。已據實調整。

**新增測試（TDD，全綠）**：`test_scored_universe.py`(6)、`test_pick_outcomes_d20.py`(4)、`test_radar_outcomes.py`(3)、`test_universe_index.py`(3) ＝ 16 新測試。全套 **1610 passed**，`test_golden_overlays.py` byte-identical 綠（證明 scorer 未被污染）。

---

## 4. 50 檢測點（PASS / FIXED / 說明）

**A. 推薦品質** 1 picks 照 score 降序 ✅ / 2 tier 門檻 90/40 ✅ / 3 d5_win_rate=45.1% 非 null ✅ / 4 avg_ret_5 顯示（注：舊 payload 偶 null，e2e 重生修正）⚠️ / 5 entry/stop/target 三價 ✅
**B. 雷達準確率** 6 雷達照推薦分排 🔧FIXED / 7 lift 0.61 警語逐字 ✅ / 8 radar_performance ledger 🔧FIXED / 9 RS≥80 門檻 ✅ / 10 領先訊號帶 backtest lift 標註 ✅
**C. 資料完整** 11 BWIBBU PE 掛 picks ✅ / 12 T86 籌碼 ✅ / 13 融資券+short% ✅ / 14 FRED 總經 banner ✅ / 15 overlay 涵蓋 leaders+scored 🔧FIXED
**D. 回測嚴謹** 16 15y CAGR/Sharpe/MaxDD ✅ / 17 Wilson-CI>base gate ✅ / 18 Bonferroni+BH ✅ / 19 DSR/PBO/CSCV（`_validation_state.json` per_signal+family）✅ / 20 ADV slippage+成本 ✅
**E. Universe 涵蓋** 21 核心 28 每日評分 ✅ / 22 機會池 ~600 組裝 ✅ / 23 全市場評分板 🔧FIXED(Fix1) / 24 回測 universe 661 ✅ / 25 無靜默截斷（limit 只 CLI smoke）✅
**F. 搜尋** 26 解析 picks ✅ / 27 解析 leaders ✅ / 28 解析 revenue ✅ / 29 全市場代號可搜 🔧FIXED(Fix3) / 30 查無結果誠實 empty-state ✅
**G. 代號+名稱** 31 picks 名稱+代碼並陳 ✅ / 32 STOCK_NAMES+opp+revenue merge ✅ / 33 有名稱不顯裸碼 ✅ / 34 .TW/.TWO 正規化 ✅ / 35 ADR vs 本地股區分（TSM=台積電 ADR）✅
**H. 回授迴路** 36 backtest→LEAD_*→strategy live ✅ / 37 06-13 demote 5 訊號落地 ✅ / 38 outcomes 回填 D+1/3/5（+D+20）✅🔧 / 39 attribution by signal/regime ✅ / 40 shadow+strategy_health ✅
**I. UI/UX** 41 scroll-snap deck ✅ / 42 tier 色+燈號 dot 色盲友善 ✅ / 43 免責逐字保留 ✅ / 44 schema_version guard ✅ / 45 ARIA+鍵盤+reduced-motion ✅
**J. Ops/可靠度** 46 daily.yml 雙 cron+skip guard ✅ / 47 每源 SKIP-not-abort ✅ / 48 失敗開 GitHub issue 告警 ✅ / 49 web_export NaN/Inf 清理(allow_nan=False)✅ / 50 monthly reval/factor-ic/overlay-readiness cron ✅

**統計**：PASS 43 ｜ FIXED 6（#6,8,15,23,29 + #38 加值）｜ WATCH 1（#4 avg_ret_5，e2e 重生驗證）。

---

## 5. 後續建議（非本次範圍）

- **雷達/D+20 樣本累積**：`radar_performance` 與 `avg_ret_20` 需數個交易日 cron 跑過才有統計力；建議 2–3 週後回看。
- **scored_universe 小型股資料薄**：缺 T86/籌碼因子的小型股 score 偏低為**設計正確**（graceful），但建議 UI 標「資料較薄」徽章避免誤讀（已加「精選分」徽章）。
- **#4 avg_ret_5 偶 null**：舊 payload 現象，e2e 重生後確認；若持續 null 需查 `summarize_hit_rate` rets 累積。

---

---

## §驗證附錄 — e2e `python main.py --web`（exit 0，2026-06-20 20:55）

| 證據 | 落地值 |
|---|---|
| Fix 1 scored_universe | `scored universe: 574 ranked, 12 on board`；payload 12 檔含 score+price（6257.TW 分138/¥235、3317.TWO 分138、DIOD 分128）|
| Fix 2 radar_performance | **n=90 / 10 日 / 勝率 61.1% / avg_ret 5.63%**（雷達準確率首次有實測值）；`_radar_outcomes/` 10 檔 |
| Fix 3 _universe.json | **2155 檔**全市場索引落地（61.6 KB）|
| Fix 4 overlay | leaders+scored tickers 併入 overlay symbols set（log 確認 T86/PE 擴涵蓋）|
| Fix 5 D+20 | `_outcomes_20/` 10 檔；d20 暫 n=0（picks 未滿 20 交易日，graceful 累積中）|
| Fix 6 雷達排序 | radarMerge 以統一 score 為主鍵排序 |
| pick_performance | d5 勝率 57.3%（75 scored / 144 picks / 12 日）|

**回歸**：全 **1610 test 綠** ｜ `test_golden_overlays.py` byte-identical 綠（scorer 未污染）｜ 16 新 TDD 測試全綠 ｜ `node --check docs/app.js` 通過。
