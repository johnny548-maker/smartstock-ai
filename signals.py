# -*- coding: utf-8 -*-
"""Early-leadership signal scanner — the 'catch it before the move' layer.

Combines four orthogonal early tells per stock:
  1. RS-line new high — relative-strength line (close ÷ benchmark) makes a new
     N-day high *while price is still below its own high* → leadership emerging
     before the price breakout is obvious (Minervini's #1 early tell).
  2. Quiet accumulation — 法人 net-buying + rising 集中度 WHILE price is flat and
     volume is below average → institutions building a position quietly.
  3. Technical setup — Stage-2 / VCP / pocket pivot (from technical_setup).
  4. Theme ride — the stock belongs to an *emerging* news theme.

GATING (per plan): a pure technical setup is noise without a *reason*. A setup
only earns a place on the early-signal board if the stock ALSO has a fundamental
reason (it is a 月營收 early-growth candidate) OR a theme reason (rides a hot
theme). RS-line-new-high and quiet accumulation are reasons in their own right.

These signals are SURFACED, not score-weighted — the backtest validates edge
before any weight is assigned (user: 要做回測才加權).
"""
import logging

import technical_setup
import volume_signals

log = logging.getLogger(__name__)

RS_LINE_WINDOW = 50         # RS line new-high lookback
QUIET_FLAT_PCT = 6.0        # |10-day return| below this = price 'flat'
QUIET_VOL_RATIO = 1.0       # recent vol below 20-day avg = 'quiet'
QUIET_WINDOW = 10


def rs_line_new_high(df, bench, window=RS_LINE_WINDOW):
    """True when the RS line (close ÷ benchmark) makes a new `window`-bar high —
    pure relative-strength leadership.

    Backtest-validated form (15y net-of-cost hardened, 65-ticker, +25%/60-bar): lift 1.23 (CI-lower 7.27% > base 6.99%).
    NOTE: an earlier version also required price to be *depressed* (≤92% of its
    high) — that gate backtested at lift 0.74 (worse than random, it caught
    laggards) and was removed. Leadership ≠ a beaten-down price."""
    try:
        if df is None or bench is None:
            return False
        s, b = df["Close"], bench["Close"]
        if len(s) <= window or len(b) <= window:
            return False
        n = min(len(s), len(b))
        rs = (s.iloc[-n:].to_numpy(dtype=float) / b.iloc[-n:].to_numpy(dtype=float))[-window:]
        return bool(rs[-1] >= rs.max() - 1e-12)
    except Exception:
        return False


def quiet_accumulation(df, chips, flat_pct=QUIET_FLAT_PCT,
                       vol_ratio=QUIET_VOL_RATIO, window=QUIET_WINDOW):
    """法人 net-buy + rising 集中度 while price flat AND volume quiet."""
    try:
        if not chips or df is None or len(df) < 21:
            return False
        conc = chips.get("conc")
        streak = chips.get("streak", 0) or 0
        accumulating = (conc is not None and conc > 0) or streak >= 1
        if not accumulating:
            return False
        close, vol = df["Close"], df["Volume"]
        ret = abs(close.iloc[-1] / close.iloc[-1 - window] - 1) * 100.0
        flat = ret < flat_pct
        ma20v = vol.rolling(20).mean().iloc[-1]
        quiet = bool(ma20v) and vol.iloc[-window:].mean() < ma20v * vol_ratio
        return bool(flat and quiet)
    except Exception:
        return False


def scan_signals(data, frames=None, chips_map=None, revenue_codes=None,
                 theme_tickers=None, names=None):
    """Scan the basket; return {per_stock, board}.

    per_stock[sym] = {rs_line, quiet, setup_score, setup_reasons, theme, fund,
                      signals:[...], count}
    board = early-signal ranking (count ≥ 2, or a gated qualifying setup),
            sorted by count desc then setup_score.
    """
    from strategy import _bench_for
    chips_map = chips_map or {}
    revenue_codes = set(revenue_codes or [])
    theme_tickers = set(theme_tickers or [])
    names = names or {}

    per_stock, board = {}, []
    for sym, df in (data or {}).items():
        code = sym.replace(".TW", "")
        bench = _bench_for(sym, frames)
        chips = chips_map.get(sym) or chips_map.get(code)
        rs = rs_line_new_high(df, bench)
        quiet = quiet_accumulation(df, chips)
        vdu = volume_signals.vdu_thrust(df)            # keyless, works on US names
        accum = volume_signals.accumulating(df)        # up/down volume ratio
        setup = technical_setup.analyze_setup(df)
        fund = sym in revenue_codes or code in revenue_codes
        theme = sym in theme_tickers or code in theme_tickers

        sigs = []
        if rs:
            sigs.append("RS線新高(領先)")
        if vdu:
            sigs.append("量縮噴出VDU(放量)")
        if accum:
            sigs.append("U/D量吸籌")
        if quiet:
            sigs.append("安靜吸籌(法人)")
        if setup["power_pivot"]:
            sigs.append("Power pivot放量突破")
        if setup["first_new_high"]:
            sigs.append("久盤後首次新高")
        if theme:
            sigs.append("主題湧現")
        if fund:
            sigs.append("月營收成長")
        # Stage-2 / VCP are LAGGING confirmations — gate on a leading reason
        has_reason = bool(rs or vdu or accum or quiet or theme or fund
                          or setup["power_pivot"] or setup["first_new_high"])
        if setup["stage2"] and has_reason:
            sigs.append("Stage2上升趨勢")
        if setup["vcp"] and has_reason:
            sigs.append("VCP收縮")

        rec = {
            "stock": sym,
            "name": names.get(sym) or names.get(sym + ".TW"),
            "rs_line": rs,
            "quiet": quiet,
            "vdu": vdu,
            "accum": accum,
            "theme": theme,
            "fund": fund,
            "setup_score": setup["setup_score"],
            "signals": sigs,
            "count": len(sigs),
        }
        per_stock[sym] = rec
        # board: needs ≥2 distinct early tells (so a lone weak signal is filtered)
        if rec["count"] >= 2:
            board.append(rec)

    board.sort(key=lambda r: (r["count"], r["setup_score"]), reverse=True)
    return {"per_stock": per_stock, "board": board}
