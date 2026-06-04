# -*- coding: utf-8 -*-
"""Stock scoring engine. Pure functions over price DataFrames + optional
institutional data. Mirrors the ChatGPT design's base factors plus the
"升級" factors (sector weight, 法人動能, risk penalties)."""
from config import (SECTOR_MAP, SECTOR_WEIGHTS, VOLATILITY_CAP, OVERHEAT_PCT,
                    MIN_BARS, MOMENTUM_LOOKBACK)


def score_stock(df, sector=None, institutional=None):
    """Score one stock.

    df: DataFrame with 'Close' and 'Volume' columns, chronological order.
    sector: optional sector label (keyed into SECTOR_WEIGHTS).
    institutional: optional {"foreign": net, "trust": net, "dealer": net}.

    Returns {"score": int, "factors": {name: points}, "insufficient": bool}.
    """
    if df is None or len(df) < MIN_BARS:
        return {"score": 0, "factors": {}, "insufficient": True}

    close = df["Close"]
    vol = df["Volume"]
    factors = {}

    # ── base factors (ChatGPT) ──────────────────────────────
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    if ma5.iloc[-1] > ma20.iloc[-1]:
        factors["趨勢(MA5>MA20)"] = 25
    if close.iloc[-1] > close.iloc[-5]:
        factors["動能(5日上漲)"] = 25
    if vol.iloc[-1] > vol.rolling(20).mean().iloc[-1]:
        factors["量能(高於20日均量)"] = 20
    if close.pct_change().std() < VOLATILITY_CAP:
        factors["波動穩定"] = 10

    # ── 升級1: 產業權重 ─────────────────────────────────────
    if sector and SECTOR_WEIGHTS.get(sector):
        factors[f"產業({sector})"] = SECTOR_WEIGHTS[sector]

    # ── 升級2: 法人動能 ─────────────────────────────────────
    if institutional:
        foreign = institutional.get("foreign", 0) or 0
        trust = institutional.get("trust", 0) or 0
        if foreign > 0:
            factors["外資買超"] = 15
        elif foreign < 0:
            factors["外資賣超"] = -20
        if trust > 0:
            factors["投信買超"] = 10

    # ── 升級3: 風險控制 ─────────────────────────────────────
    lb = min(MOMENTUM_LOOKBACK, len(close) - 1)
    if lb > 0:
        ret = (close.iloc[-1] / close.iloc[-1 - lb]) - 1
        if ret > OVERHEAT_PCT:
            factors["短期過熱(>30%)"] = -20

    return {"score": int(sum(factors.values())), "factors": factors, "insufficient": False}


def rank_stocks(data_dict, sector_map=None, institutional_map=None):
    """Score + rank a dict of {symbol: DataFrame}. Returns list sorted desc."""
    sector_map = sector_map if sector_map is not None else SECTOR_MAP
    institutional_map = institutional_map or {}
    results = []
    for sym, df in data_dict.items():
        try:
            sector = sector_map.get(sym)
            inst = institutional_map.get(sym) or institutional_map.get(sym.replace(".TW", ""))
            r = score_stock(df, sector=sector, institutional=inst)
            if r.get("insufficient"):
                continue
            results.append({
                "stock": sym,
                "score": r["score"],
                "factors": r["factors"],
                "sector": sector,
            })
        except Exception:
            # non-critical per-symbol failure → skip, don't crash the run
            continue
    return sorted(results, key=lambda x: x["score"], reverse=True)
