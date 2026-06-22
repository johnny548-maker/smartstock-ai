# -*- coding: utf-8 -*-
"""Phase 0 runner — event-study lift gate for the classic-TA EVENT signals.

Runs the SAME Wilson-CI keep/kill gate the live leadership signals passed (VDU→Thrust, U/D accum):
per signal — fired count, precision = P(+25% in 60 bars | fired), base rate, lift, Wilson-CI lower
bound, regime-split (the FLAT-regime lift is the alpha-vs-beta test), then multiple-testing
correction (Bonferroni + BH) over the family. Cache-only / offline (no network) — reuses the
15y OHLCV cache. VDU→Thrust + U/D accum are included as LIVE benchmarks (must reproduce lift>1).

KEEP (eligible for a live LEAD_* weight) = ci_beats_base AND lift>1 AND flat-regime lift>1
(not market-beta) AND fired>=FIRED_FLOOR AND survives correction. A KEEP improves pick QUALITY;
it does NOT and is NOT claimed to beat the index. A FAIL is recorded honestly and added to nothing.

Run: python run_ta_event_backtest.py [tw|us] [years]
"""
import sys
import functools

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)  # noqa: A001

import backtest
import build_ohlcv_cache as boc
import screen_price_factors as spf
import ta_event_signals as te
import volume_signals as vs
from run_backtest import SLIP_BPS, NEXT_OPEN, FEE_BPS, FIRED_FLOOR, FLAT_BETA_THRESH
from config import ADV_SLIPPAGE, BREADTH_TW, BREADTH_US, BUSTED_PEERS

HORIZON, STEP, EXPLOSIVE, MIN_BARS = 60, 10, 25.0, 200

# candidate EVENT signals (new) + live benchmarks that already passed (sanity: must show lift>1)
TA_EVENT_DEFS = {
    "Bollinger squeeze→breakout": lambda s, b: te.bollinger_squeeze_breakout(s),
    "Donchian 20d breakout":      lambda s, b: te.donchian_breakout(s, n=20),
    "KD 黃金交叉(超賣)":           lambda s, b: te.kd_golden_cross(s),
    "MACD 零軸上穿":               lambda s, b: te.macd_zero_cross_up(s),
    "[bench] VDU→Thrust":         lambda s, b: vs.vdu_thrust(s),
    "[bench] U/D量吸籌":           lambda s, b: vs.accumulating(s),
}


def _load_bench():
    """^TWII / ^GSPC from cache if present (for regime split); None → flat regime only."""
    bench = {}
    for key, tk in (("twii", "^TWII"), ("sp500", "^GSPC")):
        try:
            bench[key] = boc.load_df(tk)
        except Exception:
            bench[key] = None
    return bench


def _load_universe(market, years, full):
    """Default = the SAME leadership-gate universe (BREADTH + busted peers, cache-only, tractable)
    so the comparison to VDU/U-D is apples-to-apples. full=True → the whole survivor cache (heavy)."""
    if full:
        return spf.load_cached_prices(market.upper(), years)
    if market == "us":
        tickers = list(BREADTH_US)
    elif market == "both":
        tickers = list(BREADTH_TW) + list(BREADTH_US) + list(BUSTED_PEERS)
    else:
        tickers = list(BREADTH_TW) + list(BUSTED_PEERS)
    prices = {}
    for t in tickers:
        try:
            df = boc.load_df(t)
            if df is not None and len(df):
                prices[t] = df
        except Exception:
            continue
    return prices


def main(market="tw", years=15, full=False):
    prices = _load_universe(market, years, full)
    bench = _load_bench()
    print(f"[ta-event] market={market} years={years} universe={len(prices)} "
          f"({'FULL survivor cache' if full else 'BREADTH+busted = leadership-gate universe'} — "
          f"lift = optimistic upper bound)")
    print(f"  gate: horizon={HORIZON}d explosive>=+{EXPLOSIVE}% fee={FEE_BPS}bps next_open={NEXT_OPEN} "
          f"slip={'ADV' if ADV_SLIPPAGE else SLIP_BPS} fired_floor={FIRED_FLOOR}\n")

    bt = lambda fn: backtest.backtest_signal(
        prices, fn, bench_history=bench, horizon=HORIZON, step=STEP, explosive_pct=EXPLOSIVE,
        min_bars=MIN_BARS, next_open_fill=NEXT_OPEN, slippage_bps=SLIP_BPS, fee_bps=FEE_BPS,
        adv_slippage=ADV_SLIPPAGE)

    results = []
    for name, fn in TA_EVENT_DEFS.items():
        m = bt(fn)
        m["name"] = name
        results.append(m)
    gated = backtest.correction_gate(results, alpha=0.05, q=0.10)

    hdr = (f"{'signal':<26}{'fired':>7}{'prec':>8}{'CIlo':>8}{'base':>8}{'lift':>7}"
           f"{'flatLift':>9}{'p':>9}{'Bonf':>5}{'BH':>4}{'CI>b':>5}{'n>=100':>7}{'KEEP':>6}")
    print(hdr)
    print("-" * len(hdr))
    keepers = []
    for g in gated:
        flat_lift = g["by_regime"]["flat"]["lift"]
        thin = g["fired"] < FIRED_FLOOR
        not_beta = flat_lift > FLAT_BETA_THRESH
        keep = bool(g["kept"] and not thin and not_beta and g["lift"] > 1.0)
        if keep and not g["name"].startswith("[bench]"):
            keepers.append(g["name"])
        print(f"{g['name']:<26}{g['fired']:>7}{g['precision']:>8.2%}{g['precision_ci'][0]:>8.2%}"
              f"{g['base_rate']:>8.2%}{g['lift']:>7.2f}{flat_lift:>9.2f}{g['pvalue']:>9.4f}"
              f"{('Y' if g['bonferroni_pass'] else 'n'):>5}{('Y' if g['bh_pass'] else 'n'):>4}"
              f"{('Y' if g['ci_beats_base'] else 'n'):>5}{('Y' if not thin else 'THIN'):>7}"
              f"{('YES' if keep else 'no'):>6}")
    print()
    if keepers:
        print(f"[VERDICT] KEEP candidates (event-study lift gate passed): {keepers}")
        print("  → eligible to add as LEAD_* leadership signal (HITL + ADR). Improves pick quality, "
              "NOT an index-beating claim.")
    else:
        print("[VERDICT] No TA event signal passes the lift gate — honest negative; nothing added. "
              "(Cross-sectional TA already dead |IC|<=0.0146; event mode also no edge here.)")
    return gated


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a != "--full"]
    full = "--full" in sys.argv
    mkt = argv[0] if len(argv) > 0 else "tw"
    yrs = int(argv[1]) if len(argv) > 1 else 15
    main(mkt, yrs, full=full)
