# 📈 SmartStock Daily AI System

每天自動產生一份「全球市場新聞 + 台/美股選股 + 三大法人籌碼 + 資產配置 + 再平衡」投資日報，
寫成本機報告檔、可選擇寄到 Email，並可放上 **iPhone 主畫面 (PWA)**、由 **GitHub Actions 雲端每天自動跑**
（電腦關機也照跑），手機隨時翻看每天歷史報告。

> 📱 想直接上 iPhone？跳到 **[放上 iPhone（PWA + 雲端自動跑）](#放上-iphone-pwa--雲端自動跑)**。

落地自 ChatGPT 設計討論串的「資產配置版」，並做兩處關鍵調整：

1. **零 API key** — 原設計用 OpenAI `gpt-4o-mini` 做個股點評；本版改為**規則式中文點評**（無 LLM、離線、可重現）。所有資料來源皆免金鑰（yfinance / TWSE 公開資料 / Google News RSS）。Email 走 Gmail SMTP **應用程式密碼**（郵件憑證，非 LLM 金鑰）。
2. **補回新聞** — 原設計最終程式砍掉了「全球市場新聞」，本版用免費 RSS 補回。

---

## 資料來源（全部免金鑰）

| 區塊 | 來源 | 端點 |
|------|------|------|
| 股價 / 指數 | yfinance (Yahoo) | `Ticker.history` |
| 全球 / 台股新聞 | CNBC RSS、Google News RSS | RSS |
| 三大法人買賣超 | TWSE 公開資料 T86 | `www.twse.com.tw/rwd/zh/fund/T86` |
| 風險評級 | `^VIX` + `^TNX` | yfinance |

---

## 安裝

```bash
cd smartstock-ai
pip install -r requirements.txt
```

（本機已驗證：Python 3.11、yfinance / pandas / numpy / ta / requests / feedparser / python-dotenv 皆就緒。）

## 執行

```bash
python main.py
```

產出：
- `reports/YYYY-MM-DD.md` — Markdown 日報（一定會生成）
- `reports/YYYY-MM-DD.html` — 瀏覽器可讀版
- `smartstock.log` — 執行紀錄（含任何來源 SKIP 原因）

任一來源失效（非交易日、來源異常）會記成 **SKIP** 並繼續，報告仍會產出可用區塊。

## Email 推播（選用）

1. 複製範本：`copy .env.example .env`（PowerShell：`Copy-Item .env.example .env`）
2. Gmail 需先開**兩步驟驗證**，再到 <https://myaccount.google.com/apppasswords> 產生 16 碼**應用程式密碼**。
3. 填入 `.env`：
   ```
   EMAIL_FROM=you@gmail.com
   EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   EMAIL_TO=you@gmail.com
   ```
4. 未設定 `.env` → 自動略過 Email，只產報告檔（不報錯）。`.env` 已被 `.gitignore` 排除。

## 每天自動執行（Windows Task Scheduler）

不自動註冊。請自行在 PowerShell（系統管理員）執行：

```powershell
schtasks /Create /TN "SmartStock Daily" ^
  /TR "\"C:\Users\johnn\Downloads\Claude Test\smartstock-ai\run_daily.bat\"" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 07:30
```

- 週一至週五 07:30 執行（盤前）。法人/股價為**前一交易日收盤**結算資料。
- 移除：`schtasks /Delete /TN "SmartStock Daily" /F`

## 自訂

全部在 `config.py`：
- `STOCKS_TW` / `STOCKS_US` — 股票池
- `SECTOR_MAP` / `SECTOR_WEIGHTS` — 產業分類與加權
- `BASE_ALLOCATION` / `ALLOC_STEP` — 資產配置基準與調整幅度
- `TOP_N`、各打分門檻（`OVERHEAT_PCT`、`VOLATILITY_CAP`、`VIX_HIGH`、`RATE_HIGH`…）
- `RSS_FEEDS` — 新聞來源

`portfolio_state.json` — 填入你**目前實際**各類資產比例（總和 1.0）；再平衡建議 = 目標 − 此處。

## 評分邏輯（strategy.py）

| 因子 | 分數 |
|------|------|
| 趨勢 MA5>MA20 | +25 |
| 動能 收盤 > 5 日前 | +25 |
| 量能 > 20 日均量 | +20 |
| 波動穩定 (日報酬 std < 3%) | +10 |
| 產業權重 | AI伺服器 +20 / 半導體 +15 / 金融 +5 / 傳產 −10 |
| 外資買超 / 賣超 | +15 / −20 |
| 投信買超 | +10 |
| 短期過熱 (>30%) | −20 |

資產配置：5 類（美股成長 / 台股成長 / ETF 核心 / 加密 / 現金債券），依市場訊號（風險、美/台股動能、加密動能）±5% 調整，最後 clamp(≥0)+正規化為總和 1.0。

## 測試

```bash
python test_smartstock.py    # 19 個單元測試（純邏輯，無網路）
```

## 架構

```
main.py            主流程（抓資料→打分→點評→配置→再平衡→出報告→Email）
config.py          所有設定
data_fetcher.py    yfinance 股價 / 指數 / 動能訊號
news_digest.py     RSS 新聞摘要
institutional.py   TWSE 三大法人 T86
strategy.py        選股打分
ai_analyzer.py     規則式中文點評（無 LLM）
risk_engine.py     VIX+利率 → 風險評級
asset_allocation.py 5 類資產配置 + clamp/正規化
rebalance.py       再平衡差額
report_builder.py  組裝報告
notifier_file.py   寫 .md / .html
notifier_email.py  Gmail SMTP
web_export.py      匯出 PWA 用的 JSON + 歷史 index
docs/              PWA（GitHub Pages 由此資料夾發布）
.github/workflows/daily.yml  雲端每日排程
```

## 放上 iPhone（PWA + 雲端自動跑）

讓報告變成 iPhone 主畫面 app、雲端每天自動更新、手機隨時翻歷史 —— 全免費、免 App Store、免 Mac、免開發者帳號。

### 架構
```
GitHub Actions（雲端 cron，每天）── 跑 main.py --web → 產生 docs/data/<日期>.json
        │ commit + push
GitHub Pages（main /docs）── serve PWA + 歷史 JSON
        │ fetch
iPhone Safari「加入主畫面」── app icon、全螢幕、離線看、翻歷史
```
雲端跑分析 → 你電腦關著也有新報告。

### A. 本機先產生一筆（已做）
`python main.py --web` 會建立 `docs/data/<日期>.json` 與 `docs/data/index.json`，PWA 首次就有內容。

### B. 推上 GitHub（你的帳號，一次性）
> ⚠️ 這是**獨立的新 public repo**，跟你的「Claude Test」私人工作區無關（後者含金鑰，絕不可公開）。

本機已先 `git init` + commit 好。接著：

```bash
cd "C:\Users\johnn\Downloads\Claude Test\smartstock-ai"
# 1) 在 github.com 建一個新的 PUBLIC repo，名稱例如 smartstock-ai（不要勾 add README）
# 2) 接上並推送（把 <YOU> 換成你的 GitHub 帳號）：
git remote add origin https://github.com/<YOU>/smartstock-ai.git
git branch -M main
git push -u origin main
```
（有裝 GitHub CLI 的話可一行：`gh repo create smartstock-ai --public --source=. --push`）

### C. 開啟 GitHub Pages（一次性）
repo → **Settings → Pages** → Source 選 **Deploy from a branch** → Branch **main** / 資料夾 **/docs** → Save。
約 1 分鐘後網址：`https://<YOU>.github.io/smartstock-ai/`

### D. 確認雲端排程
repo → **Actions** 分頁 → 若提示啟用就按 Enable → 進 **SmartStock Daily** → **Run workflow** 手動跑一次測試。
之後每週一~五 18:30（台灣時間）自動跑。要改時間就改 `.github/workflows/daily.yml` 的 `cron`（UTC）。

### E.（選用）雲端也寄 Email
repo → **Settings → Secrets and variables → Actions → New repository secret**，新增：
`EMAIL_FROM`、`EMAIL_APP_PASSWORD`、`EMAIL_TO`（值同本機 `.env`）。沒設就只更新 PWA、不寄信。

### F. 加到 iPhone 主畫面
iPhone **Safari** 開 `https://<YOU>.github.io/smartstock-ai/` → 底部**分享**鈕 → **加入主畫面** → 完成。
之後點 icon 像 app 一樣全螢幕開，可離線看已下載過的報告，點任一天看歷史。

> 隱私：報告放在這個 public repo 的 Pages 上（網址不公開宣傳）。選股報告敏感度低；若要私有，改用 Cloudflare Pages（免費可私有）或 GitHub Pro。

## ⚠️ 免責

本系統由程式自動產生，僅為投資決策的**輔助資訊**，不構成任何買賣要約或投資建議。
資料來自公開來源，可能有延遲或誤差。本系統**不下單、不執行交易**。投資有風險，請自行判斷並承擔盈虧。

## 未納入（ChatGPT Level 3/4，需要再說）

回測 / Sharpe 最佳化 / 自動下單 / 自動再平衡執行。
