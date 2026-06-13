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

# FX dimension (B9) — USD/TWD spot context, keyless via yfinance 'TWD=X'.
# DISPLAY-ONLY overlay: header context + optional per-US-stock note. NEVER scored
# (no backtest gate — the Wilson-CI gate is only for weighted signals). Same keyless
# _hist path as ^TNX/^VIX. Honest framing: the PAIR (USD/TWD), not 升值/貶值.
FX_TICKER = "TWD=X"
FX_PERIOD = "1mo"

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
# Points assigned ONLY to signals whose Wilson-CI lower bound clears the base rate.
#
# 2026-06-13 RE-GATE on a 661-name universe (0050 + 中型100 + S&P500, sanitised) over
# 15y with the FULL multiple-testing family (Wilson CI + Bonferroni + BH), net-of-cost
# (next-open + 15bps slip + 30bps fee). The earlier weights were fitted on an 82-name
# universe and OVERFIT — the 661-universe re-run collapsed all but two signals:
#   KEEP:  U/D量比吸籌  lift 1.55 (was 1.39)  — PASS CI/Bonf/BH, n≈47653
#          VDU→Thrust   lift 1.61 (was CI-fail) — PASS, n≈1466 (newly earns weight)
#   KILL:  首次新高     lift 0.68 (was 2.44!) — WORSE THAN RANDOM, was the highest weight
#          Power pivot  lift 1.24 (was 2.04)  — FAIL (p≈0.05)
#          Trend Template lift 1.00 (was 1.36) — FAIL
#          Pocket pivot lift 0.99 (was 1.35)  — FAIL
#          RS線新高(純) lift 0.99 (was 1.23)  — FAIL
# The smoking gun: 首次新高 n=47→lift 2.44 on 82-univ, n=696→lift 0.68 on 661-univ — a
# textbook small-sample mirage that held the LARGEST weight (15). Demoted signals stay
# as informational overlay tells, NOT score inputs. Full table + alternatives:
# .decisions/2026-06-13-smartstock-15y-weight-gate.md
# Re-run run_backtest.py on the full universe and re-check the CI>base + Bonf + BH
# columns before changing any of these.
LEADERSHIP_WEIGHT = True
LEAD_UD_ACCUM = 8              # U/D 量比吸籌 — 15y 661-univ lift 1.55 PASS gate (keyless, works on US)
LEAD_VDU_THRUST = 10          # VDU→Thrust 量縮噴出 — 15y 661-univ lift 1.61 PASS gate → promoted 2026-06-13 (was CI-fail on 82-univ)
LEAD_FIRST_NEW_HIGH = 0       # 15y 661-univ lift 0.68 FAIL gate (was 82-univ overfit) → demoted 2026-06-13
LEAD_POWER_PIVOT = 0          # 15y 661-univ lift 1.24 FAIL gate (was 82-univ overfit) → demoted 2026-06-13
LEAD_STAGE2 = 0               # 15y 661-univ lift 1.00 FAIL gate (was 82-univ overfit) → demoted 2026-06-13
LEAD_POCKET_PIVOT = 0         # 15y 661-univ lift 0.99 FAIL gate (was 82-univ overfit) → demoted 2026-06-13
LEAD_RS_NEW_HIGH = 0          # 15y 661-univ lift 0.99 FAIL gate (was 82-univ overfit) → demoted 2026-06-13

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
#
# INTERNATIONAL additions (2026-06-09, all keyless, no API key required):
#   * reuters  — Google News RSS scoped to reuters.com (the direct Reuters RSS
#                feeds.reuters.com/reuters/businessNews is dead/bozo as of 2026-06-09;
#                GNews site:-scoped proxy returns current Reuters headlines, keyless).
#   * cnbc     — CNBC Markets RSS (direct; verified 200 OK, ~30 entries).
#   * mktwatch — MarketWatch Top Stories RSS (verified 301→content OK, ~10 entries).
# Any single bad URL will log "SKIP feed <url>: <err>" and continue (news_digest
# per-feed exception handling).  Never crashes the digest.
RSS_FEEDS = {
    "global": [
        "https://news.google.com/rss/search?q=美股+聯準會+Fed+NVIDIA+輝達&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://news.google.com/rss/search?q=美股+科技股+Nasdaq+標普500&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        # international finance: reuters (via Google News site: proxy), CNBC, MarketWatch
        "https://news.google.com/rss/search?q=site:reuters.com+markets+finance&hl=en-US&gl=US&ceid=US:en",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://feeds.marketwatch.com/marketwatch/topstories/",
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

# ── FINRA RegSHO daily short-volume overlay (B5) — keyless public .txt file ──
# cdn.finra.org posts a pipe-delimited consolidated daily short-volume file
# (Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market) ~6pm ET same day.
# Keyless public CDN file — NOT the OAuth developer.finra.org REST API. US-only
# (TW has no keyless daily short-VOLUME equivalent; 融券 is a balance, not volume).
# INFORMATIONAL OVERLAY ONLY — attached to cards like earnings_guard; never scored.
FINRA_SHVOL_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt"
FINRA_LOOKBACK_DAYS = 5         # walk back day-by-day to the latest posted file
FINRA_TIMEOUT = 15
SHORTVOL_MAX_DAYS = 30          # rolling per-symbol buffer length
SHORTVOL_TREND_WINDOW = 10      # trailing days for trend()/avg
SHORTVOL_MIN_DAYS = 3           # need this many days before trend/overlay
SHORTVOL_ELEVATED = 0.45        # short/total ≥45% → 'elevated' flag
SHORTVOL_EXTREME = 0.60         # short/total ≥60% → 'extreme' flag
# SHORTVOL_CACHE path defined below (needs WEB_DIR)

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
OPP_SCAN_LIMIT = 600         # full eligible universe: TW(400) + US CSV(~130) + anchors(~20)
                             # Raised from 260 (old artificial cap silently dropped ~9% of names).
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

# ── FRED macro spine (B6) — keyless risk-context OVERLAY, NOT a scorer ───────
# fredgraph.csv download (no key, no auth). ALWAYS pass cosd=today-45d or it
# downloads multi-decade history. Header is `observation_date,<SERIESID>` (DATE
# renamed 2024). Empty cells (holidays) AND legacy '.' missing markers dropped.
# OVERLAY-ONLY: enters payload as its own 'macro' key + a PWA banner; it is NEVER
# summed into 'risk' or any stock score (要做回測才加權). The yfinance ^VIX/^TNX
# risk input (risk_engine.market_risk) stays untouched — VIXCLS is cross-ref only.
FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_SERIES = {
    "term_spread": "T10Y2Y",      # 10Y-2Y spread (<0 = inverted curve)
    "hy_oas": "BAMLH0A0HYM2",     # HY OAS — credit stress
    "vix": "VIXCLS",              # cross-ref only (live risk input stays yfinance ^VIX)
    "dgs10": "DGS10",             # 10Y treasury yield
    "nfci": "NFCI",               # Chicago Fed NFCI — WEEKLY, Fri-stamped, ~1wk lag
}
FRED_UA = EDGAR_UA                # reuse the descriptive UA ("SmartStockDaily <email>")
MACRO_TIMEOUT = 15
MACRO_LOOKBACK_DAYS = 45          # cosd = today - this many days (avoid multi-decade dump)
MACRO_MIN_INTERVAL = 0.4          # throttle between FRED requests (mirror edgar _throttle)
# MACRO_CACHE path defined below (needs WEB_DIR)
# classify() thresholds
CREDIT_OAS_ELEVATED = 4.0         # HY OAS ≥ this → 'elevated' credit stress
CREDIT_OAS_STRESSED = 7.0         # HY OAS ≥ this → 'stressed'
NFCI_TIGHT = 0.5                  # NFCI ≥ this → 'tight' financial conditions
NFCI_LOOSE = -0.1                 # NFCI < this → 'loose'
CURVE_INVERT = 0.0                # term_spread < this → yield curve inverted

# ── Output ──────────────────────────────────────────────────
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
# PWA lives in /docs so GitHub Pages can serve it directly (Settings → Pages → main /docs)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
REVENUE_STATE = os.path.join(WEB_DIR, "data", "_revenue_state.json")
# B11 Kelly position-size GUIDANCE overlay — offline backtest writes per-signal edge
# stats here; verdict.enrich reads it to map a pick's CI-validated signal → a position
# CEILING. OVERLAY-NOT-SCORER: never an input to scoring. Absent on daily cron (the
# heavy backtest is not part of the cron) → enrich/riskPlan degrade silently to null.
KELLY_STATE = os.path.join(WEB_DIR, "data", "_kelly_state.json")
SHORTVOL_CACHE = os.path.join(WEB_DIR, "data", "_shortvol_cache.json")  # B5 FINRA RegSHO buffer
MACRO_CACHE = os.path.join(WEB_DIR, "data", "_macro_cache.json")  # B6 FRED macro 24h cache
# P2 keyless environment/overlay 24h TTL caches (sources/_cache.cached_fetch). Slow-moving
# monthly/quarterly sources (DGBAS/NDC/BLS/Treasury/SEC frames) → a 24h cache keeps the daily
# cron off the live endpoints. OVERLAY-NOT-SCORER: cached payloads feed the 'environment'
# section + per-stock overlays only, never the scorer.
ENV_TW_CACHE = os.path.join(WEB_DIR, "data", "_env_tw_cache.json")     # macro_tw industry env 24h
ENV_US_CACHE = os.path.join(WEB_DIR, "data", "_env_us_cache.json")     # macro_us BLS/FX env 24h
SEC_FRAMES_TTL_OK = True            # sec_frames caches internally via sources._cache (24h)
PORTFOLIO_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio_state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smartstock.log")

DISCLAIMER = (
    "⚠️ 免責聲明：本報告由程式自動產生，僅為投資決策的「輔助資訊」，"
    "不構成任何買賣要約或投資建議。所有數據來自公開來源，可能有延遲或誤差。"
    "投資有風險，請自行判斷並承擔盈虧。"
)
