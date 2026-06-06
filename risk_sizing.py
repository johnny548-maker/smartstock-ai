# -*- coding: utf-8 -*-
"""Risk-first position sizing + R-multiple (analyst G1).

"A great stock-picker with no risk overlay still blows up." When ~70% of signals
fail, the surviving P&L is dominated by HOW MUCH you lose on failures and size
winners — not signal selection. This turns each pick into a risk-defined plan:
per-share risk from the stop, volatility-scaled size for a fixed account-risk %,
reward:risk at entry, and a portfolio-heat cap. All keyless (price + the ATR stop
already computed). Equity-agnostic: outputs are per-unit-risk + a formula, since the
PWA doesn't know the user's account size.
"""

DEFAULT_RISK_PCT = 1.0        # account risk per trade
PORTFOLIO_HEAT_CAP = 6.0      # max total open risk across positions
MIN_RR = 2.0                  # reject/flag setups with reward:risk below this at entry


def per_share_risk(entry, stop):
    """Risk per share + that risk as a % of price. None if the stop isn't below entry."""
    if entry is None or stop is None or entry <= 0 or stop >= entry:
        return None
    risk = entry - stop
    return {"risk": round(risk, 2), "risk_pct": round(risk / entry * 100, 2)}


def position_size(equity, entry, stop, risk_pct=DEFAULT_RISK_PCT):
    """Volatility-scaled size: shares so that (entry−stop)×shares = risk_pct×equity.
    Returns {shares, notional, risk_amount, ...} or None."""
    psr = per_share_risk(entry, stop)
    if psr is None or equity <= 0:
        return None
    risk_amount = equity * risk_pct / 100.0
    shares = int(risk_amount / psr["risk"])
    return {"shares": shares, "notional": round(shares * entry, 2),
            "risk_amount": round(risk_amount, 2), "per_share_risk": psr["risk"],
            "risk_pct_of_price": psr["risk_pct"]}


def reward_risk(entry, stop, target):
    """Reward:risk at entry = (target−entry)/(entry−stop). None if degenerate."""
    if None in (entry, stop, target) or entry <= 0 or stop >= entry or target <= entry:
        return None
    return round((target - entry) / (entry - stop), 2)


def portfolio_heat(open_risk_pcts, cap=PORTFOLIO_HEAT_CAP):
    """Sum of per-position open-risk %; flag if over the heat cap (G1 portfolio budget)."""
    total = sum(open_risk_pcts or [])
    return {"total_heat": round(total, 2), "cap": cap, "within": total <= cap}


def plan(levels, risk_pct=DEFAULT_RISK_PCT, kelly_ceiling_frac=None):
    """Per-pick risk plan from levels dict (entry/stop/target band). Equity-agnostic:
    gives per-share risk %, R:R to the band target, the sizing formula, and a flag
    if R:R is below MIN_RR. Returns {} when levels are missing.

    OVERLAY-NOT-SCORER (B11): when kelly_ceiling_frac is supplied (a fraction-of-capital
    CEILING already passed the ci_beats_base weighting gate), this adds an INFORMATIONAL
    `size_ceiling_pct` = min(ATR-implied position fraction, kelly_ceiling_frac) × 100.
    The ATR-implied position fraction is risk_pct ÷ per-share-risk% — i.e. how big a
    position keeps total account risk at risk_pct given this stop's per-share risk. The
    ceiling is the MORE CONSERVATIVE of the volatility (ATR) cap and the Kelly cap. This
    is position-size GUIDANCE only — it NEVER enters scoring/ranking. When
    kelly_ceiling_frac is None the existing keys are untouched (backward-compatible).
    """
    if not levels:
        return {}
    entry = levels.get("entry")
    stop = levels.get("stop")
    band = levels.get("target_band") or []
    target = band[-1] if band else levels.get("measured_move")
    psr = per_share_risk(entry, stop)
    rr = reward_risk(entry, stop, target) if target else None
    out = {}
    if psr:
        out["risk_per_share"] = psr["risk"]
        out["risk_pct"] = psr["risk_pct"]
        out["size_formula"] = f"部位 = {risk_pct:.0f}%×總資金 ÷ {psr['risk']}（每股風險）"
    if rr is not None:
        out["rr"] = rr
        out["rr_ok"] = rr >= MIN_RR
    if kelly_ceiling_frac is not None:
        kelly_frac = max(0.0, float(kelly_ceiling_frac))
        # ATR-implied position fraction of capital: risk_pct% account-risk ÷ per-share
        # risk% → the largest position the stop allows at the fixed account-risk budget.
        if psr and psr["risk_pct"] > 0:
            atr_frac = (risk_pct / psr["risk_pct"])
            ceiling_frac = min(atr_frac, kelly_frac)
            binding = "atr" if atr_frac < kelly_frac else "kelly"
        else:
            ceiling_frac = kelly_frac
            binding = "kelly"
        out["size_ceiling_pct"] = round(ceiling_frac * 100, 2)
        out["ceiling_binding"] = binding
    return out
