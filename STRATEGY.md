# SmartStock — 最佳投資策略（嚴謹回測結論）

> 目標：在 keyless（無付費資料）約束下，找到**可重現、經樣本外驗證、誠實標註風險**的最佳策略。
> 「最佳」= 不是 backtest 報酬最大（那是過擬合），而是**樣本外（lockbox）撐得住、你抱得住**的策略。

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
