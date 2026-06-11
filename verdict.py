# -*- coding: utf-8 -*-
"""Per-stock card enrichment for the PWA (Round 3 — adopted from the 懶人分析 ref).

Turns a scored pick into a glance-able card: a 燈號 (🟢🟡🔴) + one-line verdict, a
volume ratio, multi-tier support/resistance, and a price spark series for an SVG
sparkline. All keyless/deterministic — the cron already holds the OHLCV. We borrow
the reference site's SCANNABLE structure but keep our honest numbers (ranges + the
backtest non-trigger rate), not its over-promised single-point targets.
"""
import json
import re

from indicators import pivots, dollar_adv, obv, slope
from volume_signals import acc_dist_grade
import risk_sizing

try:
    from config import KELLY_STATE
except Exception:                       # pragma: no cover — config always present in app
    KELLY_STATE = None

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


def ohlc(df, n=60):
    """Last n bars as OHLCV dicts for the interactive K-line chart (B10).

    Pure presentation — the SAME OHLCV the scorer already used, no new signal.
    Each bar: {"time":"YYYY-MM-DD","o","h","l","c" (round 2),"v" (int)}.
    Requires a DatetimeIndex (reuses spark_dates' str(idx.date()) path); returns
    [] if df is None/empty or the index isn't date-like (RangeIndex), never raises.
    """
    if df is None or not len(df):
        return []
    try:
        idx = df.index
        # date-like guard: DatetimeIndex entries expose .date(); RangeIndex ints don't
        if not hasattr(idx[-1], "date"):
            return []
        sub = df.iloc[-n:]
        bars = []
        for ts, o, h, l, c, v in zip(
            sub.index, sub["Open"], sub["High"], sub["Low"], sub["Close"], sub["Volume"]
        ):
            bars.append({
                "time": str(ts.date()),
                "o": round(float(o), 2),
                "h": round(float(h), 2),
                "l": round(float(l), 2),
                "c": round(float(c), 2),
                "v": int(round(float(v))),
            })
        return bars
    except Exception:
        return []


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


# ── B11 Kelly position-size GUIDANCE overlay (informational, never scored) ──
# Map a score_stock() factor label → its backtest (DEFS) signal name in _kelly_state.json.
# Only the CI-validated leadership signals carry a Kelly hint (the state file stores
# ci_beats_base==True only). Substring match on a stable token of the factor label, so
# the 回測lift… suffix in the factor key doesn't break the lookup.
_KELLY_FACTOR_MAP = [
    ("久盤後首次新高", "首次新高(久盤後)"),
    ("Power pivot", "Power pivot(放量突破)"),
    ("Stage2", "Trend Template"),
    ("Pocket pivot", "Pocket pivot"),
    ("U/D量", "U/D量比吸籌"),
    ("RS線新高", "RS線新高(純)"),
]

_KELLY_STATE_CACHE = None       # module-level cache (like chip_state/revenue state loads)


def _load_kelly_state():
    """Load + cache _kelly_state.json once. Returns {} on any error or when the file is
    absent (the heavy offline backtest is NOT part of the daily cron, so this artifact
    is routinely missing — enrich must degrade silently to no Kelly ceiling). OVERLAY:
    this state never enters scoring; it only sizes already-validated signals."""
    global _KELLY_STATE_CACHE
    if _KELLY_STATE_CACHE is not None:
        return _KELLY_STATE_CACHE
    state = {}
    try:
        if KELLY_STATE:
            with open(KELLY_STATE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state = loaded
    except Exception:
        state = {}
    _KELLY_STATE_CACHE = state
    return state


def _kelly_ceiling_for(factors):
    """Most CONSERVATIVE (min) kelly_capped among the pick's CI-validated triggering
    signals, mapped from its factor labels. Returns None when no signal matches or the
    state file is missing → risk_sizing.plan() then stays unchanged (no ceiling shown)."""
    state = _load_kelly_state()
    if not state or not factors:
        return None
    caps = []
    for label in factors:
        for token, sig_name in _KELLY_FACTOR_MAP:
            if token in label and sig_name in state:
                cap = state[sig_name].get("kelly_capped")
                if isinstance(cap, (int, float)):
                    caps.append(float(cap))
    return min(caps) if caps else None


def obv_flow(df):
    """量能流入(背離偏多) as an INFORMATIONAL badge — DEMOTED from scoring.

    backtest_obv.txt (15y net-of-cost) adjudicated the +10 量能流入(背離偏多) scoring branch
    a FAIL (CI-lo<=base, no edge over the base rate), so it was deleted from
    strategy.score_stock. This producer keeps the SAME predicate (slope(obv,20)>0 ∧
    slope(close,20)<=0) but emits it card-only, riding the same informational rail as the A/D
    grade (verdict 'acc_dist') / earnings — NEVER summed into score.

    Returns {'label','bullish','obv_slope','price_slope'} when the bullish divergence fires,
    else None (no divergence / too short / no data). Graceful — never raises.

    OVERLAY-NOT-SCORER: informational only; must not enter strategy.score_stock or any weight.
    """
    if df is None or getattr(df, "empty", True) or len(df) < 22:
        return None
    try:
        o = obv(df["Close"], df["Volume"])
        obv_s = slope(o, 20)
        price_s = slope(df["Close"], 20)
    except Exception:
        return None
    if not (obv_s > 0 and price_s <= 0):
        return None
    return {
        "label": "量能流入(背離偏多)",
        "bullish": True,
        "obv_slope": round(float(obv_s), 6),
        "price_slope": round(float(price_s), 6),
        "note": "OBV 上升而股價持平/下跌（吸籌背離）— 資訊性 badge，回測未過加權門檻，不進評分",
    }


def enrich(symbol, score, factors, df, levels=None, fundamental=None, overlays=None):
    """Build the card-enrichment dict attached to a pick/opportunity name.

    B11 OVERLAY-NOT-SCORER: the Kelly position-size ceiling threaded into risk_sizing.plan
    is INFORMATIONAL guidance shown beside the score+risk plan; it never enters scoring or
    ranking. Degrades silently (no ceiling) when _kelly_state.json is absent.

    fundamental: optional dict from fundamentals.build_badge() — attached as-is under the
    'fundamental' key. INFORMATIONAL ONLY; never enters scoring or ranking.

    overlays: optional list of sources/ overlay dicts (chip/法人/基本面/內部人), carried
    through verbatim under the 'overlays' key (backward-compatible default None → key
    omitted). INFORMATIONAL ONLY; the caller usually attaches them later via
    sources.overlay.attach, but accepting them here lets a one-shot build carry them too.
    NEVER enters scoring or ranking."""
    px, chg = price_change(df)
    sd, se = spark_dates(df)
    card = {
        "light": light(score),
        "verdict": verdict_line(factors),
        "price": px,
        "change_pct": chg,
        "vol_ratio": vol_ratio(df),
        "sr": sr_tiers(df),
        "spark": spark(df),
        "spark_start": sd,
        "spark_end": se,
        "ohlc": ohlc(df),           # B10 interactive K-line bars (pure presentation)
        "risk": risk_sizing.plan(levels, kelly_ceiling_frac=_kelly_ceiling_for(factors)),
        "liquidity": liquidity(symbol, df),
        "acc_dist": acc_dist_grade(df),    # informational A/D overlay (B8), never scored
        "obv_flow": obv_flow(df),          # 量能流入 informational badge (OBV-demote), never scored
        "fundamental": fundamental,        # fundamentals overlay (B12), never scored
    }
    if overlays:
        card["overlays"] = list(overlays)   # sources/ overlays sidecar (never scored)
    return card
