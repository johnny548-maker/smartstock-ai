# 2026-07-17 稽核修復 — 關鍵決策 ADR

> 背景：2026-07-17 全面稽核（findings 已驗證）→ 修復輪。本 ADR 記錄四個影響 >1 task 的
> 決策，各列 3 備選＋rationale。相關修復落點：config.py（high52 降權）、ai_analyzer.py
> （門檻一致＋風險敘事訊號化）、strategy.py/verdict.py/main.py（inputs_complete 封頂）、
> STRATEGY.md（現況更新）。測試：test_smartstock.py 300 passed。

---

## 決策 1：optimize_*.json — 回滾 vs 重跑

**背景**：2026-07-07 排程 optimize workflow 覆寫了 `optimize_*.json`，07-08 檔案與
06-21 定版（pinned universe、lockbox 只評一次、DSR 0.854/top20/252）不一致。

| 備選 | 內容 | 評估 |
|---|---|---|
| **A（採用）** | 回滾到 06-21 定版（git `3aa43349`），主迴圈執行 | 定版 = pinned-universe 可重現＋lockbox 紀律下產出；回滾成本最低、保 reproducibility |
| B | 重跑 optimize-sleeve workflow 產新定版 | lockbox held-out **只能評一次**——重跑＝再消耗 lockbox、違反 §4 方法論；且無新資料理由 |
| C | 保留 07-08 覆寫後檔案當新現況 | 事故產物非審核過的定版，無 ADR/審核軌跡，接受＝獎勵覆寫事故 |

**Rationale**：07-07 覆寫是事故非決策；接受事故產物或為此燒掉 lockbox 都比回滾差。
回滾由主迴圈統一執行（本組不碰 optimize_*.json，防並行衝突）。

## 決策 2：high52（near_high +20）處置

**背景**：「接近52週高」因子 +20，但 15 年全宇宙 cross-sectional rank-IC 實測
**-0.013（負、反預測）**（2026-07-17 稽核）。

| 備選 | 內容 | 評估 |
|---|---|---|
| **A（採用）** | 權重降 0，因子仍註冊於 FACTOR_PTS | repo 治理是 demotion-only（前例 vol_stable 10→0、5 個 leadership→0）：可逆、不動 strategy.py、0 權重不進 factors dict → chips 顯示自然消失 |
| B | 從 FACTOR_PTS / strategy.py 刪除因子 | 不可逆、破壞 golden 檢查與 A5 gate 的 reversible-demotion 設計 |
| C | 保留 +20 等下次 re-gate | 已知負 IC 因子繼續每天誤導榜單＝明知故犯 |

**Rationale**：與 vol_stable 降權完全同構（IC 負＋demotion-only 機制現成）。
near_mid(+10)/far_high(-10) 未在本輪 IC 證據內，不動。復權條件：re-gate run 通過。

## 決策 3：sec_ftd 寫入模式 — replace-tab

**背景**：SEC FTD（fails-to-deliver）資料進 sheets 的寫入模式選擇。
（執行方：fix:ci-sheets 組；本 ADR 僅記錄決策依據。）

| 備選 | 內容 | 評估 |
|---|---|---|
| **A（採用）** | replace-tab：每期整表替換 | 冪等；與來源語意一致（SEC 半月批次**全量重發**快照） |
| B | append-only 累積 | 重跑/重抓即重複列；半月快照無自然去重鍵 |
| C | merge-by-key upsert | 需設計穩定 row key（半月快照無），複雜度買不到正確性 |

**Rationale**：來源本身是全量快照 → 鏡像語意（replace）是唯一冪等且無鍵設計負擔的寫法。

## 決策 4：schedule 輸出非正典化（_scout 檔）

**背景**：決策 1 的根因防再犯——排程 optimize 不得再覆寫定版檔。

| 備選 | 內容 | 評估 |
|---|---|---|
| **A（採用）** | schedule 改寫 `optimize_*_scout.json`；正典 `optimize_*.json` 只由人工核准的 rigorous run 寫 | 保留 drift 監測價值，探索性輸出與定版隔離，升格走人工 gate |
| B | schedule 直接寫 `optimize_*.json`（現狀） | 即 07-07 事故根因；排程輸出無 HITL 就成正典＝lockbox 紀律形同虛設 |
| C | 停掉排程 optimize | 失去 drift 監測；因寫入落點錯就砍功能是過度矯正 |

**Rationale**：問題在「寫哪裡」不在「跑不跑」。scout/canonical 隔離＋人工升格閘
與 Phase 4 CI 原則（`check_factor_drift.py` 絕不自動改權、開 HITL issue）一致。
（執行方：fix:ci-sheets 組接線。）

---

## 本輪其他修復（無備選爭議，僅記錄）

- **門檻一致**：ai_analyzer 敘事門檻改 import `config.SCORE_GREEN_MIN/SCORE_AMBER_MIN`
  （舊 inline `>=70` 使 75 分同時「觀望」＋「可分批進場」）。
- **風險敘事訊號化**：固定宏觀敘事（美債/AI 過熱，每檔每天照印）移除；改由該股實際
  負向因子＋ATR 組裝；無 levels 不給捏造價位（「未達買入級，未提供進出場價位」）。
  函式拿不到的輸入（財報臨近/beta）寧缺勿假——上游 earnings_guard overlay 已在卡片層承載。
- **板塊外 verdict 封頂**：rank_stocks 帶 `inputs_complete`；機會掃描（無 chips）、
  keyless panel（連 inst 都無）、US coverage（frames only）的 🟢 封頂為 🟡 觀察＋
  `partial/reason=板塊外資料不全`；payload 加 `partial_inputs` 欄位。watchlist 核心板
  （完整輸入配置）不受影響。
