# 2026-08-03 — Lockbox 污染處置 + n_trials 不重定義

## Decision 1: 污染的 lockbox 處置 = (i) 誠實標註，不重切

**背景**：稽核 git 考古實證 terminal lockbox 視窗 [2023-06-12..2026-06-18] 被重評 ≥8 次、跨 3 個日期（audit B1-08；`audit/2026-08-03-findings.json`）——「只評一次」（ADR 2026-07-17）從未有 code 強制。

**選項**：
- (i) **採用**：承認污染——STRATEGY.md 標註 contaminated、artifact 加 `lockbox_contaminated` 旗標、`.lockbox_ledger.json` 擋未來重評。10.7% 數字保留但降級為 contaminated-holdout 證據。
- (ii) 重切新 holdout：需要凍結新視窗＋重跑全部評估＝**方法論變更**（改變所有對外統計宣稱的基底），且新視窗在資料尾端、長度不足。
- (iii) 只擋未來、不提歷史：對外數字繼續以乾淨 holdout 姿態呈現＝不誠實。

**理由**：用戶授權「積極全修但方法論翻案除外」；(ii) 是翻案、(iii) 違反本 repo 的誠實框架；(i) 保留證據價值同時止血。NO-GO verdict 不依賴此數字（由多重獨立負結果承載），污染不改變結論方向。

## Decision 2: n_trials 不重定義，改落敏感度表

**背景**：combo 記 `n_trials=1`（pre-registered 宣稱），DSR 0.998 在 n*≈5 翻面（B1-16）。「effective trials 該怎麼算」是方法論核心。

**選項**：(a) 重定義 n_trials（如記入全部 sleeve 組態搜尋數）＝改變 gate 嚴格度＝方法論變更；(b) **採用**：n_trials 照舊、artifact 內嵌 `dsr_sensitivity` 表（n∈{1,3,5,10,45,100} 的 DSR＋n*），讓讀者自行判斷 PASS 的脆弱度；(c) 什麼都不做。

**理由**：呼叫方對搜尋規模的單方控制權是所有過擬合檢定的共同結構極限（[[Overfit-Gate-Trust-Boundary-and-Adversarial-TDD-Review]]）——重定義單一數字解決不了，透明化脆弱度才是 process 層正解。

**Effect**：`89dcc381b`（ledger＋sensitivity）、`fb22d8bf4`（STRATEGY 標註）。**Reversal**：刪 ledger 檔＋還原 STRATEGY 段落；重切 holdout 屬新決策需另立 ADR。
