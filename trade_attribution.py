# -*- coding: utf-8 -*-
"""Iteration-4 / avenue D — OWN-TRADE ATTRIBUTION (your private edge, not a market strategy).

Per wiki [[stock-return-information-priority-map]] Tier 1: the highest-confidence, lowest-cost
edge is fixing YOUR OWN behavioral/timing/cost leaks, measured on YOUR OWN fills — not chasing
public-factor alpha (which this session proved keyless markets don't offer). This module computes
the Tier-1 diagnostics from a broker transaction log:

  • behavior gap   actual money-weighted return (XIRR) vs a buy-&-hold-same-capital benchmark
                   (Dalbar QAIB: the average investor underperforms their own funds via timing)
  • win/loss + expectancy   W·R − (1−W); the disposition effect (Odean 1998) shows as
                   small avg-win / large avg-loss (cutting winners, riding losers)
  • hold-time buckets   realised return grouped by holding days (<30/30-90/90-180/>180)
                   to expose "selling winners too early"

Needs YOUR data: a broker 對帳單 / transaction export. Parser is column-name driven (broker
formats vary; 國泰 BOM-as-Big5 caveat in [[reference_broker_csv_parse]]). The MATH below is
unit-tested on synthetic trades so it is correct the moment real data arrives.
"""
import math

import numpy as np
import pandas as pd

# expected normalized trade columns after parsing
COLS = ["ticker", "side", "date", "price", "shares"]   # side ∈ {"buy","sell"}


def _xnpv(rate, cfs):
    """NPV of dated cashflows (list of (date, amount)) at an annual `rate`."""
    t0 = min(d for d, _ in cfs)
    return sum(a / (1.0 + rate) ** ((d - t0).days / 365.0) for d, a in cfs)


def xirr(cfs, lo=-0.999, hi=10.0):
    """Money-weighted annualised return (internal rate of return on dated cashflows). Bisection —
    robust where Newton diverges. cfs = [(date, amount)]; outflows negative, inflows positive."""
    cfs = [(pd.Timestamp(d), float(a)) for d, a in cfs if a != 0]
    if len(cfs) < 2 or all(a <= 0 for _, a in cfs) or all(a >= 0 for _, a in cfs):
        return None
    flo, fhi = _xnpv(lo, cfs), _xnpv(hi, cfs)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        fm = _xnpv(mid, cfs)
        if abs(fm) < 1e-7:
            return mid
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2.0


def pair_round_trips(trades):
    """FIFO-match buys↔sells per ticker → closed round trips with realised P&L + holding days.
    Returns DataFrame[ticker, buy_date, sell_date, shares, buy_px, sell_px, ret, hold_days]."""
    out = []
    for tk, g in trades.sort_values("date").groupby("ticker"):
        lots = []   # open buy lots: [shares, price, date]
        for _, r in g.iterrows():
            if r["side"] == "buy":
                lots.append([r["shares"], r["price"], pd.Timestamp(r["date"])])
            elif r["side"] == "sell":
                qty = r["shares"]
                while qty > 1e-9 and lots:
                    lot = lots[0]
                    take = min(qty, lot[0])
                    out.append({"ticker": tk, "buy_date": lot[2], "sell_date": pd.Timestamp(r["date"]),
                                "shares": take, "buy_px": lot[1], "sell_px": r["price"],
                                "ret": r["price"] / lot[1] - 1.0,
                                "hold_days": (pd.Timestamp(r["date"]) - lot[2]).days})
                    lot[0] -= take
                    qty -= take
                    if lot[0] <= 1e-9:
                        lots.pop(0)
    return pd.DataFrame(out)


def win_loss_expectancy(rt):
    """From round trips → win rate, avg win, avg loss, payoff ratio, expectancy per trade."""
    if rt is None or rt.empty:
        return {}
    wins, losses = rt[rt["ret"] > 0]["ret"], rt[rt["ret"] <= 0]["ret"]
    w = len(wins) / len(rt)
    aw = float(wins.mean()) if len(wins) else 0.0
    al = float(losses.mean()) if len(losses) else 0.0
    payoff = (aw / abs(al)) if al else float("inf")
    return {"n": len(rt), "win_rate": round(w, 3), "avg_win": round(aw, 4),
            "avg_loss": round(al, 4), "payoff": round(payoff, 3) if math.isfinite(payoff) else None,
            "expectancy": round(w * aw + (1 - w) * al, 4)}


def hold_time_buckets(rt, edges=(30, 90, 180)):
    """Mean realised return by holding-period bucket — exposes 'selling winners too early'."""
    if rt is None or rt.empty:
        return {}
    labels = [f"<{edges[0]}d"] + [f"{edges[i]}-{edges[i+1]}d" for i in range(len(edges) - 1)] + [f">{edges[-1]}d"]
    bins = [-1] + list(edges) + [10**9]
    rt = rt.copy()
    rt["bucket"] = pd.cut(rt["hold_days"], bins=bins, labels=labels)
    g = rt.groupby("bucket", observed=False)["ret"]
    return {str(k): {"n": int(v), "mean_ret": round(float(m), 4)}
            for k, v, m in zip(g.size().index, g.size().values, g.mean().values)}


def behavior_gap(trades, end_prices, end_date):
    """Behavior gap measured over the SAME window [first buy, end_date], so it captures the cost of
    your sell TIMING rather than rewarding fast turnover (raw XIRR does the latter — a quick +10%
    annualises huge). Both legs share identical BUY cashflows; they differ only in what you hold at
    end_date:
      • actual   : your sell proceeds sit as idle cash to end_date (conservative: proceeds you took
                   out and did NOT redeploy into these names) + any still-open position marked at end
      • buy&hold : you never sold — every share you bought is marked at end_prices
    gap = actual_xirr − buyhold_xirr; gap<0 = your selling/timing destroyed value vs holding.
    `end_prices` = {ticker: last_price}. Assumption (idle cash at 0%) documented for honesty."""
    end_date = pd.Timestamp(end_date)
    buys = [(r["date"], -r["price"] * r["shares"]) for _, r in trades[trades["side"] == "buy"].iterrows()]
    if not buys:
        return {"actual_xirr": None, "buyhold_xirr": None, "behavior_gap": None}
    sell_proceeds = float(sum(r["price"] * r["shares"]
                              for _, r in trades[trades["side"] == "sell"].iterrows()))
    net, bought = {}, {}
    for _, r in trades.iterrows():
        d = 1 if r["side"] == "buy" else -1
        net[r["ticker"]] = net.get(r["ticker"], 0) + d * r["shares"]
        if r["side"] == "buy":
            bought[r["ticker"]] = bought.get(r["ticker"], 0) + r["shares"]
    open_mv = float(sum(sh * end_prices[tk] for tk, sh in net.items()
                        if abs(sh) > 1e-9 and tk in end_prices))
    bh_mv = float(sum(sh * end_prices[tk] for tk, sh in bought.items() if tk in end_prices))
    actual_xirr = xirr(buys + [(end_date, sell_proceeds + open_mv)])
    bh_xirr = xirr(buys + [(end_date, bh_mv)])
    gap = (actual_xirr - bh_xirr) if (actual_xirr is not None and bh_xirr is not None) else None
    return {"actual_xirr": actual_xirr, "buyhold_xirr": bh_xirr, "behavior_gap": gap}
