# SmartStock — 最佳投資策略（嚴謹回測結論）

> 目標：在 keyless（無付費資料）約束下，找到**可重現、經樣本外驗證、誠實標註風險**的最佳策略。
> 「最佳」= 不是 backtest 報酬最大（那是過擬合），而是**樣本外（lockbox）撐得住、你抱得住**的策略。

---

## 1. 策略本體：vol-targeted 橫斷面動能

- **選股**：12-1 動能（過去 12 個月報酬，跳過最近 1 個月）取前 N 名（cross-sectional momentum）。
- **部位**：constant-vol 縮放（cMOM）— 用 σ 目標 / 已實現波動 clamp 槓桿（0.5×–1.5×），把組合波動穩在 σ 目標附近。
- **再平衡**：季度 / 月度。
- **冠軍配置（pinned 定版 run 確認中）**：`vol_target=True, σ≈0.20, top_n≈20–30, rebalance=quarterly, lookback=252`。
  > 兩次 live-universe run 給 top30/top20 漂移 → 已**釘住 universe**（commit `universe_full_market.csv`）讓 champion 可重現。定版數字落地後更新此行。

## 2. 誠實的 forward 預期（lockbox，只評一次）

| 指標 | 數字（TW, held-out 2023-06→2026-06） |
|---|---|
| CAGR | **~28%** |
| MaxDD | **~-32%** |
| calmar | ~0.88 |

- 這是**真終端 holdout**（lockbox）數字，不是 in-sample backtest 的 ~48%。
- pooled-OOS（跨窗）calmar ~3.5 與 lockbox ~0.9 有落差 = 仍偏樂觀，**信 lockbox**。
- **-32% 回撤是動能的結構性入場費**，不是 bug。

## 3. 風險配置選單（讓策略「抱得住」的真正旋鈕）

回撤改不動（見 §5 趨勢濾網實證有害）→ 降回撤的唯一可靠槓桿是**降配置**：把資金分成「動能 sleeve + 現金」。
近似線性換（現金 ~1.5% 無風險、零回撤）：

| 動能配置 | ~CAGR | ~MaxDD | 適合 |
|---|---|---|---|
| 100% | 28% | -32% | 高風險耐受、長線、不看帳 |
| 75% | ~21% | ~-24% | 積極 |
| **50%** | **~15%** | **~-16%** | **多數人的甜蜜點** |
| 33% | ~10% | ~-11% | 保守 |
| 25% | ~8% | ~-8% | 很保守 |

> 選你**晚上睡得著**的回撤列，往左讀 CAGR = 你的長期期望報酬。-32% 抱不住就別放 100%。

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
