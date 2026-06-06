# -*- coding: utf-8 -*-
"""Per-stock card enrichment for the PWA (Round 3 — adopted from the 懶人分析 ref).

Turns a scored pick into a glance-able card: a 燈號 (🟢🟡🔴) + one-line verdict, a
volume ratio, multi-tier support/resistance, and a price spark series for an SVG
sparkline. All keyless/deterministic — the cron already holds the OHLCV. We borrow
the reference site's SCANNABLE structure but keep our honest numbers (ranges + the
backtest non-trigger rate), not its over-promised single-point targets.
"""
import re

from indicators import pivots, dollar_adv
from volume_signals import acc_dist_grade
import risk_sizing

THIN_FLOOR_USD = 3_000_000     # < $3M average daily $-volume = hard to act on at size
THIN_FLOOR_TWD = 50_000_000    # < NT$50M/day
CAP_PCT = 0.01                 # rule of thumb: one name's position ≤ ~1% of ADV

GREEN_MIN = 90          # score ≥ → 🟢
AMBER_MIN = 40          # 40-89 → 🟡 ; <40 → 🔴
_PAREN = re.compile(r"（.*?）|\(.*?\)")


def light(score):
    if score >= GREEN_MIN:
        return "green"
    if score >= AMBER_MIN:
        return "amber"
    return "red"


def _clean(label):
    return _PAREN.sub("", label).strip()


def verdict_line(factors):
    """One short Chinese sentence from the dominant factors."""
    factors = factors or {}
    pos = [_clean(k) for k, v in factors.items() if v > 0]
    neg = [_clean(k) for k, v in factors.items() if v < 0]
    parts = pos[:2]
    if neg:
        parts.append("注意" + neg[0])
    return "、".join(parts) if parts else "訊號平淡，觀望"


def vol_ratio(df, recent=5, base=20):
    """量比(5日)：recent-day avg volume vs base-day avg, as a % (None if too short)."""
    if df is None or len(df) < base:
        return None
    v = df["Volume"]
    b = v.iloc[-base:].mean()
    if not b:
        return None
    return int(round((v.iloc[-recent:].mean() / b - 1) * 100))


def sr_tiers(df, k=3):
    """Multi-tier support/resistance from swing pivots: up to 2 resistances above
    and 2 supports below the current price (+ strong support = lowest pivot)."""
    if df is None or len(df) < 2 * k + 2:
        return None
    close = float(df["Close"].iloc[-1])
    lows, highs = pivots(df, k)
    res = sorted({round(p, 2) for _, p in highs if p > close})[:2]
    sup = sorted({round(p, 2) for _, p in lows if p < close}, reverse=True)[:2]
    strong = round(min((p for _, p in lows), default=close), 2)
    return {"price": round(close, 2), "resistance": res, "support": sup,
            "strong_support": strong}


def spark(df, n=60):
    """Last n closes for an SVG sparkline (rounded floats)."""
    if df is None or not len(df):
        return []
    return [round(float(c), 2) for c in df["Close"].iloc[-n:]]


def price_change(df):
    """(current price, day change %) from the last two closes."""
    if df is None or not len(df):
        return None, None
    px = round(float(df["Close"].iloc[-1]), 2)
    if len(df) < 2:
        return px, None
    prev = float(df["Close"].iloc[-2])
    return px, (round((px / prev - 1) * 100, 2) if prev else None)


def spark_dates(df, n=60):
    """(start_date, end_date) strings for the sparkline x-axis, or (None, None)."""
    if df is None or not len(df) or not hasattr(df.index, "__getitem__"):
        return None, None
    try:
        idx = df.index
        end = idx[-1]
        start = idx[-min(n, len(df))]
        return str(getattr(start, "date", lambda: start)()), str(getattr(end, "date", lambda: end)())
    except Exception:
        return None, None


def liquidity(symbol, df):
    """Capacity read (analyst G13): average daily $-volume + a ~1%-ADV position cap +
    a thin flag. Keyless/pure — a microcap can be a perfect setup yet impossible to
    enter at size without moving it. None if no volume history."""
    adv = dollar_adv(df)
    if adv is None:
        return None
    tw = symbol.endswith((".TW", ".TWO"))
    floor = THIN_FLOOR_TWD if tw else THIN_FLOOR_USD
    return {
        "adv": round(adv),
        "cur": "NT$" if tw else "$",
        "cap": round(adv * CAP_PCT),       # max position before you ARE the volume
        "thin": bool(adv < floor),
    }


def enrich(symbol, score, factors, df, levels=None):
    """Build the card-enrichment dict attached to a pick/opportunity name."""
    px, chg = price_change(df)
    sd, se = spark_dates(df)
    return {
        "light": light(score),
        "verdict": verdict_line(factors),
        "price": px,
        "change_pct": chg,
        "vol_ratio": vol_ratio(df),
        "sr": sr_tiers(df),
        "spark": spark(df),
        "spark_start": sd,
        "spark_end": se,
        "risk": risk_sizing.plan(levels),
        "liquidity": liquidity(symbol, df),
        "acc_dist": acc_dist_grade(df),    # informational A/D overlay (B8), never scored
    }
