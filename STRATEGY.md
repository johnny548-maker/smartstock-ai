# SmartStock — 最佳投資策略（嚴謹回測結論）

> 目標：在 keyless（無付費資料）約束下，找到**可重現、經樣本外驗證、誠實標註風險**的最佳策略。
> 「最佳」= 不是 backtest 報酬最大（那是過擬合），而是**樣本外（lockbox）撐得住、你抱得住**的策略。

---

## ⚠️ 現況更新（2026-07-17 稽核）

1. **07-07 排程覆寫事故**：排程的 optimize workflow 於 2026-07-07 覆寫了 `optimize_*.json`，使 07-08 之後的檔案內容與本文件引用的 **06-21 定版**（DSR 0.854 / top20 / lookback252）不一致。下方 06-21 現況段引用的數字**以定版為準**。處置：主迴圈將把 `optimize_*.json` 回滾到 06-21 定版（git `3aa43349`）；schedule 之後改寫 `optimize_*_scout.json`（探索性輸出，非正典——由 fix:ci-sheets 組接線）。決策紀錄：`.decisions/2026-07-17-audit-remediation.md`。
2. **未驗證因子清單（誠實標註）**：主榜單 scorer 的 `sector`（產業權重）、`inst_foreign_buy/sell`（外資買賣超）、`inst_trust_buy`（投信買超）、`chip_conc_*`（籌碼集中度）、`streak`（連買）**不在 15y IC harness 覆蓋範圍**——keyless 歷史籌碼/法人資料不可得，無法回測。這些權重是啟發式設定，非回測驗證；價格類因子（trend/momentum/volume/rs/high52/rsi/obv）才有 15y IC 數字。
3. **主榜單公式無組合級回測**：個別因子過 IC / event-study gate ≠ 多因子「加總分數」這個組合在歷史上有效。組合級回測需要歷史籌碼/法人時間序列（keyless 不可得）→ **做不了、也不假裝做過**。主榜單定位維持「觀察名單/輔助」，非可回測驗證的交易策略（與 §8 Phase 1 結論一致）。
4. **2026-07-17 稽核修復（一致性）**：(a) `near_high`（接近52週高 +20）15y rank-IC **-0.013（負）** → 循 demotion-only 治理降 0（前例：vol_stable、5 個 leadership 訊號）；(b) `ai_analyzer` 敘事門檻改 import config 燈號常數（舊 `>=70` 使 75 分同時「觀望」+「可分批進場」）；(c) 風險敘事改由該股實際負向訊號組裝（舊版每天每檔照印同一句美債/AI 宏觀敘事）、無 levels 不再捏造 -7%/+15~25% 價位；(d) 板塊外（機會掃描/keyless panel/US coverage）verdict 因輸入不全（無 sector/chips 或連 inst 都無）**封頂「觀察」**，不再與 watchlist 共用絕對 ≥90 買入門檻虛標 🟢。

## ⚠️ 現況（2026-06-21 稽核，先讀這段）

1. **這個 champion 沒過品質 gate → 不該上線。** DSR=0.854 < 0.95 門檻（`dsr_pass=false`），walk-forward `stable=false`，lockbox calmar 0.55 vs pooled-OOS 1.79 崩盤。`run_optimize` 自己標：「likely in-sample mirage — do NOT promote to live weights」。**所以下面的冠軍是「目前最不爛的候選」，不是「可部署的最佳策略」。**
2. **這個 champion 沒有接進 live app。** `optimize_tw.json` 是死檔，全 repo 無任何 `.py` 讀它。你 PWA 每天看到的選股跑的是**另一套** `strategy.rank_stocks` 多因子 scorer（config.py FACTOR_PTS），跟這個動能 champion **無關**。
3. **app 裡的「動能 lens」也是舊的**（top10/lookback126/σ0.15，舊 Calmar-winner），不是這個 champion（top20/252/σ0.20），且只是旁觀資訊、不驅動選股。
4. **多因子擴張（LOWVOL+STREV+MOM inverse-vol 組合，預登記）結果 = 真實但贏不了大盤。**
   - **分散有效**：組合 DSR **0.998**（動能單體 0.854→組合 0.998）+ PBO 0.293 + lockbox CAGR **10.7%** / MaxDD **-21.4%** → **不是過擬合、是真的、抱得住**（回撤從動能單體 -36% 降到 -21%）。
   - **但贏不了大盤**：SPA p=**0.170**（沒顯著贏 0050，需<0.05）+ FLAT-regime lift **0.813**（平盤時輸大盤 = 偏 beta 非 alpha）→ **5 關 FAIL**。
5. **最終誠實結論（目標答案）：keyless TW 沒有顯著贏大盤的主動策略 → 最佳投資策略 = 買大盤指數（0050）。** 嚴謹擴張搜尋（含分散組合）證實：能做出真實、抱得住、~10.7%/-21% 的因子組合，**但統計上贏不了被動持有指數**。這是合法的科學終點（factor-zoo：多數異象經多重檢定後失效），照 ADR §8 **不鬆 gate 硬湊**。

6. **迭代2（籌碼面+基本面，keyless，2026-06-22）= 同樣負結果。** 用戶問「加市值/營收/籌碼/法人買賣超等更多資料能不能找到 alpha」。3 輪窮盡研究（官方→FinMind→社群）+ 對抗驗證（16 findings 全修）後，誠實答案：
   - **可 keyless 多年回測的新因子（純 TWSE 官方直連）= 法人流(instflow, T86) + 價值(value, BWIBBU PB/殖利率)。**
   - **全 14 年史（2012-2026, 3664日×144檔）跨橫斷面 rank-IC ≈ 0**：instflow **+0.0002**、value **+0.0141**，**皆未過 0.05 floor → IC 篩 FAIL，沒有任何因子進 gate**（`optimize_tw_aux.json`、`run_aux_combo.py`、ADR `2026-06-22`）。
   - **真卡死、誠實排除**：券商分點(付費/無 keyless 多年存檔)、多年籌碼集中度(TDCC 只1年/FinMind 付費)、margincontra(無 keyless 流通股；絕不用成交量當 proxy 虛胖過關)。
   - 結論：**籌碼面+基本面因子在 keyless TW 同樣無 cross-sectional edge**，強化「最佳=被動 0050」。`run_aux_combo`/`build_aux_panels` 是 research-only，**從不接進 live app**（同 optimize_tw.json）。

> **白話**：你問「最佳策略並 push 進 app 了嗎」→ 答案是 **(a) 嚴謹找過了（價格+籌碼+基本面三類都試），(b) 沒有主動策略過 gate 贏大盤，(c) 所以最佳 = 買 0050 被動持有。** 你的 11-因子 app 可當「觀察名單/輔助」，但別當成能贏大盤的 alpha 機器。

---

## 1. 策略本體：vol-targeted 橫斷面動能

- **選股**：12-1 動能（過去 12 個月報酬，跳過最近 1 個月）取前 N 名（cross-sectional momentum）。
- **部位**：constant-vol 縮放（cMOM）— 用 σ 目標 / 已實現波動 clamp 槓桿（0.5×–1.5×），把組合波動穩在 σ 目標附近。
- **再平衡**：季度 / 月度。
- **冠軍配置（pinned 定版 run 確認 ✅，可重現）**：`vol_target=True, σ=0.20, top_n=20, rebalance=quarterly, lookback=252, trend_ma=null`。
  > 釘住 universe 後，定版 run 與前一跑數字**位元相同** = 可重現已實證（run 27906100225）。`trend_ma=null` = 嚴謹流程選了「無趨勢濾網」（見 §5）。

## 2. 誠實的 forward 預期（lockbox，只評一次）

| 指標 | LOCKBOX（held-out 2023-06→2026-06，只評一次） | pooled-OOS（跨窗，搜尋區） |
|---|---|---|
| CAGR | **19.73%** | 29.21% |
| MaxDD | **-35.72%** | -16.35% |
| calmar | **0.55** | 1.79 |

- 信**左欄（lockbox）= 真終端 holdout** 的誠實 forward，不是 in-sample backtest 的 ~48%。
- pooled-OOS（29% / -16%）比 lockbox（20% / -36%）漂亮 = 搜尋偏樂觀；held-out 2023-26 對 TW 動能是**較硬的 regime**（報酬低、回撤深）。**以 lockbox 為準**。
- **-36% 回撤是動能的結構性入場費**，不是 bug。誠實期望 ≈ **20% CAGR、近 -36% 回撤**。

## 3. 風險配置選單（讓策略「抱得住」的真正旋鈕）

回撤改不動（見 §5 趨勢濾網實證有害）→ 降回撤的唯一可靠槓桿是**降配置**：把資金分成「動能 sleeve + 現金」。
近似線性換（現金 ~1.5% 無風險、零回撤）：

| 動能配置 | ~CAGR | ~MaxDD | 適合 |
|---|---|---|---|
| 100% | 19.7% | -35.7% | 高風險耐受、長線、不看帳 |
| 75% | ~15.2% | ~-26.8% | 積極 |
| **50%** | **~10.6%** | **~-17.9%** | **多數人的甜蜜點** |
| 33% | ~7.5% | ~-11.8% | 保守 |
| 25% | ~6.0% | ~-8.9% | 很保守 |

> 數字基於定版 lockbox（CAGR 19.73% / MaxDD -35.72%）+ 現金 1.5% 線性混合近似。選你**晚上睡得著**的回撤列，往左讀 CAGR = 長期期望報酬。-36% 抱不住就別放 100%。

## 4. 為什麼這是「嚴謹」的（方法論）

- **Walk-forward + 真 lockbox**：策略只在 lockbox 前的資料搜尋；冠軍在從沒碰過的尾段評一次 = 誠實 forward。
- **pooled-OOS 選擇**：用跨窗**池化報酬**算單一目標選冠軍（非 mean-of-per-fold-calmar，後者被低-DD fold 灌水誤選）。
- **DSR + PBO 閘**：每個調參算一次 trial，用 Deflated Sharpe 折扣選擇偏誤；PBO/CSCV 測過擬合機率。
- **可重現**：universe 釘住，同輸入同輸出。

## 5. 嚴謹流程**否決**了什麼（同樣是結論）

| 否決項 | 證據 |
|---|---|
| **「不斷調到 in-sample 報酬最大」** | 過擬合坑；repo 實證 2.44→0.68（in-sample→OOS 崩）。最大化 OOS 非 in-sample。 |
| **200d 趨勢濾網（time-series momentum）** | TW 實證**有害**：48 配對 **0/48** 壓低 DD，平均**加深 8% 回撤** + 砍 ~10% CAGR。MA 落後 → 賣在低點、TW V 型反彈追高 → 自製回撤。美股大盤有效不代表 TW。 |

## 6. 限制（keyless 誠實標註）

- **survivorship**：universe = 現在成分股，無 keyless 下市股歷史 → backtest 偏樂觀。真 point-in-time 需付費歷史成分數據集。
- **美股全市場**：~5653 名 15y cache 增量建置中（warm-us-cache.yml，每 6h 一片），滿了才有 US 定版。

## 7. 怎麼跑

```
# 定版 TW（pinned universe，可重現）
gh workflow run optimize-sleeve.yml -f sleeve=tw -f objective=calmar -f universe=full -f rigorous=true
# 重建 universe（會破壞可重現性，僅在要更新成分時）
#   ... -f rebuild_universe=true
```

結果寫 `optimize_tw.txt` / `optimize_tw.json`（嚴謹版區塊 = pooled-OOS vs lockbox）。

## 8. 迭代總結（2026-06-22）— 15y 驗證版報告 + 誠實框架

用戶重新框定「找最佳可部署分析接進 app + 每日報告」後（非贏大盤 alpha）：

- **Phase 0 事件型 TA 訊號**：布林 squeeze→突破 / Donchian / KD / MACD 跑 event-study lift gate（VDU/U-D 過關的同條路；gate 自證：U/D 重現 lift 1.35 KEEP）。全 FAIL：布林 lift 1.03/平盤 0.68(beta)、Donchian 0.89、MACD 0.84；**KD 黃金交叉(超賣) 最強 lift 1.21、贏 base、非 beta，但敗 Bonferroni**(p=0.0134>0.0125)。橫斷面 TA 早證死(|IC|≤0.0146)。**不加任何訊號。**（`ta_event_signals.py`/`run_ta_event_backtest.py`）
- **Phase 1 scorer 15y 重驗**：8 個 base 因子全 rank-IC<0.05 但 reward 因子 top-decile edge 全正(rs 4.18..obv 2.29)→ 照 vol_stable 雙指標規則**無新降權**→ scorer 不動、**零 golden churn**。誠實：現有選股是 15y 驗證過的**觀察名單/輔助**，從未 SPA-vs-指數測過，**非贏大盤機器**。
- **Phase 2/3 已接進 app（informational，非 scorer）**：`validated_portfolio.py` 把預註冊 LOWVOL+STREV+MOM 風控組合(`optimize_tw.json[combo]`：DSR 0.998✓/PBO 0.293✓/lockbox 10.7%/-21%✓ 但 **SPA p=0.17✗/flat 0.81✗→overall 未過**)當每日 track，**並列被動 0050/SPY 基準**。實證：被動 0050 15y CAGR **11.4%** > 組合 lockbox 10.7%——**被動贏報酬，組合只贏回撤**。PWA「驗證組合」tab + 永遠在頂「不承諾贏大盤」banner + 每因子 15y-IC 信心。
- **Phase 4 CI**：`check_factor_drift.py` 月度監控因子跨越保留/降權邊界→開 HITL issue，**CI 絕不自動改權**。

**底線不變**：keyless 公開資料**無**「過 gate 且大贏大盤」策略（含這輪布林等 TA 全證偽）。最誠實「最佳策略」= 被動指數；風控組合是可選主動 sleeve（抱得住但不贏指數）。ADR `.decisions/2026-06-22-scorer-15y-revalidation.md`。
