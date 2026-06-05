# -*- coding: utf-8 -*-
"""Stock scoring engine. Pure functions over OHLCV DataFrames + optional
institutional data + optional benchmark (for relative strength).

Factors: trend, momentum, volume, volatility, sector, institutional (liquidity-
gated), relative-strength-vs-index, 52-week-high proximity, RSI-14 (replaces the
old crude overheat rule), OBV volume-price divergence.
"""
from config import (SECTOR_MAP, SECTOR_WEIGHTS, STOCK_NAMES, VOLATILITY_CAP, MIN_BARS,
                    RS_WINDOW, RS_STRONG, HIGH_WINDOW, NEAR_HIGH, NEAR_MID, FAR_HIGH,
                    RSI_WINDOW, RSI_OVERBOUGHT, RSI_OVERSOLD,
                    INST_RATIO_FULL, INST_RATIO_HALF,
                    CONC_HIGH, CONC_MID, STREAK_MIN,
                    LEADERSHIP_WEIGHT, LEAD_FIRST_NEW_HIGH, LEAD_POWER_PIVOT,
                    LEAD_STAGE2, LEAD_UD_ACCUM, LEAD_POCKET_PIVOT, LEAD_RS_NEW_HIGH,
                    BUCKET_SCORING, BUCKET_CAPS, BUCKET_IC_WEIGHTS)
from indicators import rsi as rsi_ind, obv as obv_ind, slope
from technical_setup import analyze_setup
from signals import rs_line_new_high
from volume_signals import accumulating as ud_accumulating


# Factor → bucket classifier (de-collinearization). Rules are ordered; first match
# wins. Each scored factor label maps to exactly one orthogonal bucket.
_BUCKET_RULES = [
    ("meanrev", ("RSI", "遠離52週高")),
    ("relstr", ("相對強", "相對弱", "RS線新高")),
    ("trend", ("趨勢", "動能", "接近52週高", "逼近52週高", "Stage2", "久盤後首次新高")),
    ("volacc", ("量能", "波動穩定", "量價背離", "籌碼", "連買", "Pocket", "U/D量", "Power pivot")),
    ("fund", ("產業", "外資", "投信")),
]


def _bucket_of(label):
    for bucket, keys in _BUCKET_RULES:
        if any(k in label for k in keys):
            return bucket
    return "fund"


def _bucket_score(factors):
    """Group factors into buckets, clamp each to ±BUCKET_CAPS (so the over-counted
    trend factor can't dominate), then IC-weight and sum. Returns (score, subtotals)."""
    buckets = {}
    for label, pts in factors.items():
        buckets[_bucket_of(label)] = buckets.get(_bucket_of(label), 0) + pts
    capped = {}
    for b, v in buckets.items():
        cap = BUCKET_CAPS.get(b, 999)
        capped[b] = max(-cap, min(cap, v))
    score = int(round(sum(BUCKET_IC_WEIGHTS.get(b, 1.0) * v for b, v in capped.items())))
    return score, capped


def _rs_excess(df, bench, window):
    """Stock return minus benchmark return over `window` bars, or None."""
    try:
        s, b = df["Close"], bench["Close"]
        if len(s) <= window or len(b) <= window:
            return None
        s_ret = s.iloc[-1] / s.iloc[-1 - window] - 1
        b_ret = b.iloc[-1] / b.iloc[-1 - window] - 1
        return s_ret - b_ret
    except Exception:
        return None


def score_stock(df, sector=None, institutional=None, bench=None, chips=None):
    """Score one stock. Returns {score, factors, insufficient}."""
    if df is None or len(df) < MIN_BARS:
        return {"score": 0, "factors": {}, "insufficient": True}

    close, vol = df["Close"], df["Volume"]
    factors = {}

    # ── base factors ────────────────────────────────────────
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    up_trend = ma5.iloc[-1] > ma20.iloc[-1]
    if up_trend:
        factors["趨勢(MA5>MA20)"] = 25
    if close.iloc[-1] > close.iloc[-5]:
        factors["動能(5日上漲)"] = 25
    ma20v = vol.rolling(20).mean().iloc[-1]
    if ma20v and vol.iloc[-1] > ma20v:
        factors["量能(高於20日均量)"] = 20
    if close.pct_change().std() < VOLATILITY_CAP:
        factors["波動穩定"] = 10
    if sector and SECTOR_WEIGHTS.get(sector):
        factors[f"產業({sector})"] = SECTOR_WEIGHTS[sector]

    # ── institutional, liquidity-gated (法人買超佔量比) ──────
    if institutional:
        foreign = institutional.get("foreign", 0) or 0
        trust = institutional.get("trust", 0) or 0
        ratio = abs(foreign) / ma20v if ma20v else 0
        mult = 1.0 if ratio >= INST_RATIO_FULL else (0.5 if ratio >= INST_RATIO_HALF else 0.0)
        if foreign > 0 and mult:
            factors["外資買超"] = int(15 * mult)
        elif foreign < 0 and mult:
            factors["外資賣超"] = int(-20 * mult)
        if trust > 0:
            factors["投信買超"] = 10

    # ── relative strength vs index ──────────────────────────
    if bench is not None:
        rsx = _rs_excess(df, bench, RS_WINDOW)
        if rsx is not None:
            if rsx > RS_STRONG:
                factors["相對強弱(強於大盤)"] = 20
            elif rsx > 0:
                factors["相對強弱(優於大盤)"] = 15
            else:
                factors["相對弱勢(弱於大盤)"] = -10

    # ── 52-week-high proximity (George & Hwang 2004) ────────
    win = min(HIGH_WINDOW, len(df))
    hi = df["High"].rolling(win).max().iloc[-1]
    if hi and hi > 0:
        near = close.iloc[-1] / hi
        if near >= NEAR_HIGH:
            factors["接近52週高"] = 20
        elif near >= NEAR_MID:
            factors["逼近52週高"] = 10
        elif near < FAR_HIGH:
            factors["遠離52週高"] = -10

    # ── RSI-14 (replaces old >30%-gain overheat rule) ───────
    r = rsi_ind(close, RSI_WINDOW)
    if r > RSI_OVERBOUGHT:
        factors["RSI過熱(>75)"] = -15
    elif r < RSI_OVERSOLD and up_trend:
        factors["RSI回檔買點"] = 5

    # ── OBV volume-price divergence ─────────────────────────
    o = obv_ind(close, vol)
    obv_s, price_s = slope(o, 20), slope(close, 20)
    if obv_s > 0 and price_s <= 0:
        factors["量能流入(背離偏多)"] = 10
    elif price_s > 0 and obv_s < 0:
        factors["量價背離(出貨警示)"] = -15

    # ── 籌碼集中度 + 外資投信連買 streak (cross-run buffer) ──
    if chips:
        conc = chips.get("conc")
        if conc is not None:
            if conc >= CONC_HIGH:
                factors["籌碼集中(法人吸籌)"] = 15
            elif conc >= CONC_MID:
                factors["籌碼集中(偏多)"] = 8
            elif conc < 0:
                factors["籌碼分散(法人調節)"] = -15
        st = chips.get("streak", 0) or 0
        if st >= STREAK_MIN:
            factors[f"外資投信連買{st}日"] = 20

    # ── leadership patterns (CI-validated weights, hardened 15y run_backtest) ──
    # ONLY signals whose Wilson-CI lower bound cleared the base rate over 15y survive
    # here. VCP and VCP∧Stage2 were REMOVED (the 5y lift-2.0 was a regime illusion;
    # 15y+CI rejected them). Each factor is additive — these are orthogonal early
    # tells; a name lighting up several is a genuine leader. See config LEAD_*.
    if LEADERSHIP_WEIGHT:
        setup = analyze_setup(df)
        if setup["first_new_high"]:
            factors["久盤後首次新高(回測lift2.4)"] = LEAD_FIRST_NEW_HIGH
        if setup["power_pivot"]:
            factors["Power pivot放量突破(回測lift2.0)"] = LEAD_POWER_PIVOT
        if setup["stage2"]:
            factors["Stage2上升趨勢(回測lift1.36)"] = LEAD_STAGE2
        if setup["pocket_pivot"]:
            factors["Pocket pivot吸籌(回測lift1.35)"] = LEAD_POCKET_PIVOT
        if ud_accumulating(df):
            factors["U/D量吸籌(回測lift1.39)"] = LEAD_UD_ACCUM
        if bench is not None and rs_line_new_high(df, bench):
            factors["RS線新高領先(回測lift1.23)"] = LEAD_RS_NEW_HIGH

    if BUCKET_SCORING:
        score, buckets = _bucket_score(factors)
        return {"score": score, "factors": factors, "buckets": buckets, "insufficient": False}
    return {"score": int(sum(factors.values())), "factors": factors,
            "buckets": None, "insufficient": False}


def _bench_for(sym, frames):
    if not frames:
        return None
    return frames.get("twii") if sym.endswith(".TW") else frames.get("sp500")


def rank_stocks(data_dict, sector_map=None, institutional_map=None, frames=None, chips_map=None):
    """Score + rank {symbol: DataFrame}. frames = {twii, sp500} for RS;
    chips_map = {sym: {conc, streak}} for 籌碼 factors."""
    sector_map = sector_map if sector_map is not None else SECTOR_MAP
    institutional_map = institutional_map or {}
    chips_map = chips_map or {}
    results = []
    for sym, df in data_dict.items():
        try:
            sector = sector_map.get(sym)
            inst = institutional_map.get(sym) or institutional_map.get(sym.replace(".TW", ""))
            chips = chips_map.get(sym) or chips_map.get(sym.replace(".TW", ""))
            r = score_stock(df, sector=sector, institutional=inst,
                            bench=_bench_for(sym, frames), chips=chips)
            if r.get("insufficient"):
                continue
            results.append({
                "stock": sym,
                "name": STOCK_NAMES.get(sym) or STOCK_NAMES.get(sym + ".TW") or None,
                "score": r["score"],
                "factors": r["factors"],
                "sector": sector,
            })
        except Exception:
            continue
    return sorted(results, key=lambda x: x["score"], reverse=True)
