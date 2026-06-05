# -*- coding: utf-8 -*-
"""Market regime — IBD-style Distribution-Day / Follow-Through-Day exposure dial.

The system's only top-down gate was risk_engine.market_risk (a coarse VIX+rate tier),
and breadth measures PARTICIPATION not the institutional-SELLING regime. The analyst's
#1 overlay (and a research-converged P0): an explicit risk-on/off + exposure multiplier
that GATES the whole report — ~75% of breakouts fail in a market downtrend, so firing
leadership signals with equal confidence below the 200DMA sells users into bull setups
during bears. Pure index OHLCV (^TWII/^GSPC already fetched). Keyless, deterministic.
"""
DD_DROP = -0.002          # close down ≥0.2% = a down day
DD_WINDOW = 25            # rolling sessions to count distribution days
FTD_MIN_GAIN = 0.0125     # follow-through day: index up ≥1.25% on higher volume
BASE_EXPOSURE = {"uptrend": 100, "neutral": 60, "downtrend": 25}


def distribution_day(df, i):
    """Bar i closed down ≥0.2% on higher volume than the prior bar (institutional selling)."""
    if df is None or i < 1 or i >= len(df):
        return False
    c, v = df["Close"], df["Volume"]
    down = (c.iloc[i] / c.iloc[i - 1] - 1) <= DD_DROP
    higher_vol = v.iloc[i] > v.iloc[i - 1]
    return bool(down and higher_vol)


def distribution_count(df, window=DD_WINDOW):
    """Distribution days in the trailing `window` sessions."""
    if df is None or len(df) < 2:
        return 0
    n = len(df)
    return sum(1 for i in range(max(1, n - window), n) if distribution_day(df, i))


def follow_through_day(df, window=10):
    """A rally-attempt confirmation in the last `window` bars: up ≥1.25% on higher vol."""
    if df is None or len(df) < window + 1:
        return False
    c, v = df["Close"], df["Volume"]
    for i in range(len(df) - window, len(df)):
        if i >= 1 and (c.iloc[i] / c.iloc[i - 1] - 1) >= FTD_MIN_GAIN and v.iloc[i] > v.iloc[i - 1]:
            return True
    return False


def trend_state(df):
    """uptrend / neutral / downtrend from price vs MA50/MA200."""
    if df is None or len(df) < 50:
        return "neutral"
    c = df["Close"]
    last = float(c.iloc[-1])
    ma50 = float(c.rolling(50).mean().iloc[-1])
    ma200 = float(c.rolling(min(200, len(c))).mean().iloc[-1])
    if last > ma50 > ma200:
        return "uptrend"
    if last < ma50 and last < ma200:
        return "downtrend"
    return "neutral"


def exposure_dial(df):
    """0-100% exposure from trend state minus a distribution-day penalty (clusters of
    4-5 DD in ~4wk signal a top). Returns {exposure, dd_count, trend, ftd, label}."""
    state = trend_state(df)
    dd = distribution_count(df)
    penalty = max(0, dd - 2) * 12                      # >2 DD starts cutting exposure
    exp = max(0, min(100, BASE_EXPOSURE[state] - penalty))
    label = "risk-on" if exp >= 70 else ("caution" if exp >= 40 else "risk-off")
    return {"exposure": exp, "dd_count": dd, "trend": state,
            "ftd": follow_through_day(df), "label": label}


def market_regime(frames):
    """Combine TW + US dials → the CONSERVATIVE (min) exposure gate. None if no frames."""
    out = {}
    for k in ("twii", "sp500"):
        df = (frames or {}).get(k)
        if df is not None and len(df) >= 50:
            out[k] = exposure_dial(df)
    if not out:
        return None
    exp = min(v["exposure"] for v in out.values())
    label = "risk-on" if exp >= 70 else ("caution" if exp >= 40 else "risk-off")
    return {"exposure": exp, "label": label, "detail": out}
