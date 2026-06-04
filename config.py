# -*- coding: utf-8 -*-
"""SmartStock Daily AI System — central config.

No secrets here. Email credentials are read from environment / .env at runtime
(see notifier_email.py). This file holds tunable knobs only.
"""
import os

# ── Stock pools ─────────────────────────────────────────────
# Taiwan (yfinance uses the .TW suffix for TWSE listings)
STOCKS_TW = [
    "2330.TW",  # 台積電  semiconductor
    "2317.TW",  # 鴻海    AI server / EMS
    "3231.TW",  # 緯創    AI server
    "2382.TW",  # 廣達    AI server
    "2308.TW",  # 台達電  components
    "2454.TW",  # 聯發科  semiconductor
    "2891.TW",  # 中信金  finance
]
STOCKS_US = ["NVDA", "AMD", "TSM", "MSFT", "QQQ"]

# ── Sector mapping + weights (ChatGPT 升級1: 產業權重) ────────
SECTOR_MAP = {
    "2330.TW": "半導體",
    "2454.TW": "半導體",
    "2317.TW": "AI伺服器",
    "3231.TW": "AI伺服器",
    "2382.TW": "AI伺服器",
    "2308.TW": "電子",
    "2891.TW": "金融",
    "NVDA": "AI伺服器",
    "AMD": "半導體",
    "TSM": "半導體",
    "MSFT": "AI伺服器",
    "QQQ": "ETF",
}
SECTOR_WEIGHTS = {
    "AI伺服器": 20,
    "半導體": 15,
    "金融": 5,
    "傳產": -10,
    # unlisted sectors → 0
}

# ── Asset-allocation base weights (5 classes) ───────────────
BASE_ALLOCATION = {
    "US_GROWTH": 0.30,
    "TW_GROWTH": 0.25,
    "ETF_CORE": 0.25,
    "CRYPTO": 0.10,
    "CASH_BOND": 0.10,
}
ALLOC_STEP = 0.05  # how much each signal shifts a class

# ── Market index tickers (yfinance) ─────────────────────────
INDICES = {
    "twii": "^TWII",     # 加權指數
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "vix": "^VIX",       # volatility / risk
    "tnx": "^TNX",       # 10Y US treasury yield (value = yield * 10)
    "btc": "BTC-USD",
}

# ── Scoring thresholds ──────────────────────────────────────
MOMENTUM_LOOKBACK = 63          # ~3 trading months for STRONG/WEAK momentum
VOLATILITY_CAP = 0.03           # daily pct-change std below this = stable (+points)
OVERHEAT_PCT = 0.30             # >30% gain in lookback → risk penalty
MIN_BARS = 20                   # need at least this many bars to score
TOP_N = 3                       # how many picks get full AI commentary

# ── Risk engine thresholds (ChatGPT risk_engine) ───────────
VIX_HIGH = 20.0
RATE_HIGH = 4.5

# ── News RSS feeds (all keyless) ────────────────────────────
# Google News RSS is the universal multilingual fallback.
RSS_FEEDS = {
    "global": [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",      # CNBC top news
        "https://news.google.com/rss/search?q=stock+market+Fed+Nvidia&hl=en-US&gl=US&ceid=US:en",
    ],
    "tw": [
        "https://news.google.com/rss/search?q=台股+外資+台積電&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    ],
}
NEWS_PER_FEED = 5

# ── TWSE institutional (三大法人) open data — keyless JSON ───
# T86 = per-stock 三大法人買賣超日報. 'rwd' endpoint returns {stat,fields,data}.
# Today's data posts only after market close → walk back to last trading day.
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_TIMEOUT = 15
TWSE_LOOKBACK_DAYS = 7

# ── Output ──────────────────────────────────────────────────
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
# PWA lives in /docs so GitHub Pages can serve it directly (Settings → Pages → main /docs)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
PORTFOLIO_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio_state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smartstock.log")

DISCLAIMER = (
    "⚠️ 免責聲明：本報告由程式自動產生，僅為投資決策的「輔助資訊」，"
    "不構成任何買賣要約或投資建議。所有數據來自公開來源，可能有延遲或誤差。"
    "投資有風險，請自行判斷並承擔盈虧。"
)
