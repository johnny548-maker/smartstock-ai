# -*- coding: utf-8 -*-
"""SmartStock Daily AI System — central config.

No secrets here. Email credentials are read from environment / .env at runtime
(see notifier_email.py). This file holds tunable knobs only.
"""
import os

# ── Stock pools ─────────────────────────────────────────────
# Taiwan (yfinance uses the .TW suffix for TWSE listings)
STOCKS_TW = [
    "2330.TW", "2317.TW", "2454.TW", "2308.TW", "2382.TW", "3231.TW",
    "2303.TW", "3711.TW", "2379.TW", "3008.TW",          # 半導體/光學
    "2891.TW", "2882.TW", "2881.TW", "2412.TW",          # 金融/電信
    "2002.TW", "2207.TW", "2603.TW", "6505.TW",          # 傳產/航運
]
STOCKS_US = ["NVDA", "AMD", "TSM", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "AVGO", "QQQ"]

# Display names (中文/公司名) — shown as "名稱 (代碼)" everywhere
STOCK_NAMES = {
    "2330.TW": "台積電", "2317.TW": "鴻海", "2454.TW": "聯發科",
    "2308.TW": "台達電", "2382.TW": "廣達", "3231.TW": "緯創",
    "2303.TW": "聯電", "3711.TW": "日月光投控", "2379.TW": "瑞昱", "3008.TW": "大立光",
    "2891.TW": "中信金", "2882.TW": "國泰金", "2881.TW": "富邦金", "2412.TW": "中華電",
    "2002.TW": "中鋼", "2207.TW": "和泰車", "2603.TW": "長榮", "6505.TW": "台塑化",
    "NVDA": "NVIDIA", "AMD": "AMD", "TSM": "台積電 ADR", "MSFT": "微軟",
    "AAPL": "蘋果", "GOOGL": "Alphabet", "AMZN": "亞馬遜", "META": "Meta",
    "AVGO": "博通", "QQQ": "Nasdaq100 ETF",
}


def stock_name(symbol):
    """Return '名稱 (代碼)'; works for bare TWSE codes too (e.g. '2330')."""
    name = STOCK_NAMES.get(symbol) or STOCK_NAMES.get(symbol + ".TW")
    return f"{name} ({symbol})" if name else symbol

# ── Sector mapping + weights (ChatGPT 升級1: 產業權重) ────────
SECTOR_MAP = {
    "2330.TW": "半導體", "2454.TW": "半導體", "2303.TW": "半導體",
    "3711.TW": "半導體", "2379.TW": "半導體", "3008.TW": "半導體",
    "2317.TW": "AI伺服器", "3231.TW": "AI伺服器", "2382.TW": "AI伺服器",
    "2308.TW": "電子",
    "2891.TW": "金融", "2882.TW": "金融", "2881.TW": "金融", "2412.TW": "金融",
    "2002.TW": "傳產", "2207.TW": "傳產", "2603.TW": "傳產", "6505.TW": "傳產",
    "NVDA": "AI伺服器", "AMD": "半導體", "TSM": "半導體", "MSFT": "AI伺服器",
    "AVGO": "半導體", "AAPL": "AI伺服器", "GOOGL": "AI伺服器", "AMZN": "AI伺服器",
    "META": "AI伺服器", "QQQ": "ETF",
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

# ── Data window ─────────────────────────────────────────────
STOCK_PERIOD = "1y"             # need ~252 bars for 52-week-high factor (keyless)

# ── Scoring thresholds ──────────────────────────────────────
MOMENTUM_LOOKBACK = 63          # ~3 trading months for STRONG/WEAK momentum
VOLATILITY_CAP = 0.03           # daily pct-change std below this = stable (+points)
MIN_BARS = 20                   # need at least this many bars to score
TOP_N = 3                       # how many picks get full commentary + price levels
DISPLAY_N = 12                  # how many ranked picks to show in report/PWA

# ── Market breadth basket (broad, representative — close-only, 3mo) ─────────
# Computed over a wide sample so 參與度 is meaningful (the core watchlist alone
# is too AI/semi-biased). Tickers only; no per-name treatment.
BREADTH_PERIOD = "3mo"
BREADTH_TW = [
    "2330.TW", "2317.TW", "2454.TW", "2308.TW", "2382.TW", "3231.TW", "2303.TW",
    "3711.TW", "2379.TW", "3008.TW", "2357.TW", "2395.TW", "4938.TW", "2412.TW",
    "2891.TW", "2882.TW", "2881.TW", "2886.TW", "2884.TW", "2885.TW", "2880.TW",
    "2892.TW", "2887.TW", "2890.TW", "2883.TW", "5880.TW", "2002.TW", "2207.TW",
    "2603.TW", "2609.TW", "2615.TW", "6505.TW", "1303.TW", "1301.TW", "1326.TW",
    "2912.TW", "1216.TW", "2105.TW", "2474.TW", "3034.TW", "2345.TW", "3045.TW",
    "1101.TW", "2327.TW", "3037.TW",
]
BREADTH_US = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA",
    "AMD", "TSM", "NFLX", "JPM", "V", "XOM", "JNJ", "WMT", "PG", "COST",
]
# Busted-momentum stress set (G3 survivorship counter). These boomed then gave it
# all back in the 2021-22 unwind — including them in the backtest universe puts
# loser-paths back that a survivor-only list hides. NOTE: a partial fix — these still
# trade (true delisted names vanish from yfinance), so it lowers but does not remove
# the survivorship bias. Used only by the offline backtest gate, never the daily run.
BUSTED_PEERS = [
    "PTON", "BYND", "SPCE", "NKLA", "PLUG", "FSLY", "ROKU", "ZM",
    "DOCU", "PINS", "SNAP", "AFRM", "UPST", "CVNA", "CHPT", "RIVN", "LCID",
]
BREADTH_HEALTHY = 0.60          # ≥60% above MA20 → 健康
BREADTH_WEAK = 0.40             # <40% → 轉弱

# Relative strength vs index (RS): excess return over benchmark, 60-day
RS_WINDOW = 60
RS_STRONG = 0.05                # >+5% over index → top tier

# 52-week-high proximity (George & Hwang 2004)
HIGH_WINDOW = 252
NEAR_HIGH = 0.95                # within 5% of 52wk high → top tier
NEAR_MID = 0.90
FAR_HIGH = 0.75                 # >25% below 52wk high → penalty

# RSI-14 (Wilder) — replaces the old crude >30%-gain overheat rule
RSI_WINDOW = 14
RSI_OVERBOUGHT = 75
RSI_OVERSOLD = 35

# 法人買超佔量比 gate: |net| / 20-day avg volume → scales institutional points
INST_RATIO_FULL = 0.30          # ≥30% of avg vol → full weight
INST_RATIO_HALF = 0.10          # 10-30% → half; <10% → noise (zero)

# 籌碼集中度 (cumulative foreign net / volume) + 外資投信連買 streak
CONC_HIGH = 0.05                # ≥5% over window → strong accumulation
CONC_MID = 0.02
STREAK_MIN = 3                  # ≥3 consecutive sync-buy days → bonus

# ── Leadership patterns (backtest-validated weights) ────────────────────────
# Points assigned ONLY to signals whose Wilson-CI lower bound clears the base rate
# over a HARDENED 15-year walk-forward (next-open fill + 15bps slippage, regime-
# split, base rate 6.99%). The 5-year numbers were regime-illusory: VCP∧Stage2 read
# lift 2.0 on 5y but 1.27 with CI-lower 6.31% < base over 15y → REJECTED. VCP-alone
# (0.89) and VDU-thrust (CI fail) also rejected. Re-run run_backtest.py and re-check
# the CI>base column before changing any of these.
LEADERSHIP_WEIGHT = True
LEAD_FIRST_NEW_HIGH = 15       # 久盤後首次新高 — 15y lift 2.44, CI✓ (rare, n=47)
LEAD_POWER_PIVOT = 18          # 放量突破事件 — 15y lift 2.04, CI✓ (n=112)
LEAD_STAGE2 = 12               # Trend Template — 15y lift 1.36, CI✓ (broad, recall 35%)
LEAD_UD_ACCUM = 8              # U/D 量比吸籌 — 15y lift 1.39, CI✓ (keyless, works on US)
LEAD_POCKET_PIVOT = 8          # pocket pivot — 15y lift 1.35, CI✓
LEAD_RS_NEW_HIGH = 5           # RS-line new high (pure) — 15y lift 1.23, CI✓ (modest)
# REJECTED (failed CI-lower>base over 15y, do NOT weight): VCP-alone (0.89),
# VCP∧Stage2 (1.27/CI6.31), VDU-thrust (1.46/CI6.19), PowerPivot∧TT (1.82/CI6.58,n=63)

# ATR price levels (stop / target)
ATR_WINDOW = 14
ATR_STOP_MULT = 2.0             # stop = close − 2×ATR
RR_TARGET = 2.5                 # target = close + 2.5×risk
STOP_FLOOR_PCT = 0.93           # stop never tighter-risk than -7% (cap risk)

# ── Risk engine thresholds (ChatGPT risk_engine) ───────────
VIX_HIGH = 20.0
RATE_HIGH = 4.5

# ── News RSS feeds (all keyless) ────────────────────────────
# Google News RSS is the universal multilingual fallback.
# zh-Hant Google News for global markets → Chinese headlines that keep English
# proper nouns (Nvidia / Fed / S&P) intact — no translation engine, keyless.
RSS_FEEDS = {
    "global": [
        "https://news.google.com/rss/search?q=美股+聯準會+Fed+NVIDIA+輝達&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://news.google.com/rss/search?q=美股+科技股+Nasdaq+標普500&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
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

# ── 月營收 (monthly revenue) — the LEADING fundamental spine (council #1) ───
# t187ap05_L = ALL listed companies' latest-month revenue in ONE keyless JSON
# (當月 + 去年當月 → YoY instantly). Scans the whole market without per-stock fetch.
TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
REV_MIN_YOY = 20.0          # only surface ≥20% YoY as an early-growth candidate
EARLY_CANDIDATE_N = 15      # how many revenue candidates to show
REV_ACCEL_MONTHS = 3        # YoY accelerating = strictly rising over N months
REV_BUFFER_MONTHS = 18      # keep this many months of YoY per stock
REV_YOY_CEILING = 300.0     # >300% monthly YoY = base-effect/lumpy recognition, not growth
REV_MIN_REVENUE = 100000.0  # 當月營收下限(千元≈1億)，排除微型股 YoY 噪音
# lumpy / non-comparable monthly revenue → exclude (建商認列、金融、證券)
REV_EXCLUDE_INDUSTRIES = ["建材營造", "金融保險", "證券", "存託憑證"]

# ── Factor de-collinearization (Round 2 P1-E) ───────────────────────────────
# 6 additive factors (MA5>MA20, 5d momentum, RS-vs-index, 52wk-high proximity,
# Stage-2, RS-line-new-high) are hats on ONE latent 'price trend' factor → the raw
# sum mostly says 'already went up'. Bucket them, CAP each bucket so trend cannot
# dominate, then combine. Factor COMPUTATION is unchanged (golden); only the
# aggregation changes. Enabled ONLY after run_rank_ic.py shows the composite beats
# the flat additive score (ship gate: top-decile fwd-return edge + positive FLAT regime).
BUCKET_SCORING = False
BUCKET_CAPS = {"trend": 30, "volacc": 25, "relstr": 25, "meanrev": 15, "fund": 20}
BUCKET_IC_WEIGHTS = {"trend": 1.0, "volacc": 1.0, "relstr": 1.0, "meanrev": 1.0, "fund": 1.0}

# ── Opportunity universe (Round 2 — decoupled scan-set, sees small/mid-caps) ─
# The watchlist (28) is what we track; the OPPORTUNITY universe is what we SCAN to
# surface names we don't yet hold (AAOI/NVTS-class). Keyless: US from a committed
# CSV, TW from TWSE/TPEx open-data company lists ranked by dollar-volume.
_HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_US_CSV = os.path.join(_HERE, "universe_us.csv")
SUPPLY_CHAIN_MAP = os.path.join(_HERE, "supply_chain_map.json")
TWSE_LIST_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TWSE_DAYALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_LIST_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TPEX_DAYALL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
HTTP_UA = {"User-Agent": "smartstock-ai/1.0 (github actions; contact johnny548@gmail.com)"}
OPP_TW_CAP_N = 400            # top-N TW names by dollar-volume eligible for the scan
OPP_SCAN_LIMIT = 260         # hard cap on OHLCV names fetched per daily run (429/runtime guard)
OPP_PERIOD = "2y"            # need >252 bars for the 12-month cross-sectional RS-Rating window
OPP_BATCH = 45               # yf.download batch size (Yahoo 429 mitigation)
OPP_TOP_DISPLAY = 15         # opportunity early-leaders to surface in the report
OPP_RS_MIN = 80              # cross-sectional RS-Rating floor for a leadership candidate
# SEC EDGAR — keyless US fundamental spine (quarterly revenue acceleration)
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
EDGAR_UA = "SmartStockDaily johnny548@gmail.com"   # SEC requires a descriptive UA (blank → 403)
EDGAR_CACHE = os.path.join(_HERE, ".cache", "edgar")
EDGAR_REVENUE_CONCEPTS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                          "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"]

# ── Output ──────────────────────────────────────────────────
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
# PWA lives in /docs so GitHub Pages can serve it directly (Settings → Pages → main /docs)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
REVENUE_STATE = os.path.join(WEB_DIR, "data", "_revenue_state.json")
PORTFOLIO_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio_state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smartstock.log")

DISCLAIMER = (
    "⚠️ 免責聲明：本報告由程式自動產生，僅為投資決策的「輔助資訊」，"
    "不構成任何買賣要約或投資建議。所有數據來自公開來源，可能有延遲或誤差。"
    "投資有風險，請自行判斷並承擔盈虧。"
)
