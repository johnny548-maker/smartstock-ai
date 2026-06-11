# -*- coding: utf-8 -*-
"""OBV-divergence WEIGHTING-GATE ADJUDICATION — 15y net-of-cost backtest (task W6).

strategy.py:135-141 attaches a LIVE score weight to OBV volume-price divergence:
    量能流入(背離偏多) = +10   when slope(obv,20)>0 and slope(close,20)<=0
    量價背離(出貨警示) = -15   when slope(close,20)>0 and slope(obv,20)<0
…with NO backtest evidence — a direct violation of the project 要做回測才加權
(Wilson-CI-lower > base) rule that already culled VCP / VCP∧Stage2 from the live
scorer. This script is the MISSING adjudication. It re-uses the existing hardened
harness (backtest.backtest_signal: next-open fill, slippage 15bps, round-trip fee
30bps net-of-cost, Wilson CI, UP/FLAT/DOWN regime split, fired count) and emits a
PASS/FAIL verdict per the same keep/kill ruler the price signals went through.

Run: python run_backtest_obv.py [years] [horizon] [explosive_pct]
Default: 15y / 60-bar / +25%.

  • Bullish (量能流入/背離偏多, +10): tested as a positive signal — does firing it
    precede explosive (>=+25%) forward returns above the base rate? PASS only if
    Wilson-CI-lower > base AND fired >= 100 AND FLAT-regime lift > 1.0.
  • Bearish (量價背離/出貨警示, -15): tested as an AVOID filter — does the avg 60-bar
    forward return AFTER the warning come in BELOW the universe base? A negative gap
    means avoiding names that fire it adds value (filter benefit), independent of the
    explosive-hit precision.

VERDICT → action:
  • Bullish PASS  → recommend KEEP the +10 live weight; commit this evidence file.
  • Bullish FAIL  → recommend DEMOTE to an informational overlay-not-scorer badge
                    (delete the strategy.py lines, surface as a card overlay).

OVERLAY-NOT-SCORER / NO-LIVE-TOUCH: this script READS strategy's predicate logic but
NEVER imports or mutates strategy.py / config.LEAD_*. It only writes evidence.
Still survivorship-biased (yfinance survivors + BUSTED_PEERS stress set): every lift
is an OPTIMISTIC UPPER BOUND.
"""
import sys
import logging

# CJK signal names crash the default cp1252 Windows console on print.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import data_fetcher
import backtest
from indicators import obv as obv_ind, slope
from config import BREADTH_TW, BREADTH_US, BUSTED_PEERS

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

SLIP_BPS = 15.0          # ~0.15% each side (bid/ask + impact) — mirrors run_backtest.py
FEE_BPS = 30.0           # round-trip commission + TW transaction tax (net-of-cost, G9)
NEXT_OPEN = True         # fill at next open (signal fires on close) — no exec look-ahead (G4)
INCLUDE_BUSTED = True    # add boomed-then-busted peers to fight survivorship (G3)

OBV_SLOPE_WINDOW = 20    # MUST match strategy.py:137 slope(o,20)/slope(close,20)
FIRED_FLOOR = 100        # < this many fires → n-thin, INELIGIBLE for live weight (same as early-board)
FLAT_BETA_THRESH = 1.0   # FLAT-regime lift must exceed this (>1.0) to be alpha not market-beta


# ── PREDICATES — byte-for-byte the strategy.py:135-141 divergence expressions, wrapped
#    in the (s, b) OHLCV signature DEFS uses (b ignored: pure OHLCV). Exception-safe so a
#    short/empty slice during the walk-forward yields a graceful False, never a raise. ───
def obv_bullish_divergence(s, b=None):
    """量能流入(背離偏多): OBV rising while price flat/falling — strategy.py:138.
    True iff slope(obv,20) > 0 and slope(close,20) <= 0."""
    try:
        if s is None or len(s) < OBV_SLOPE_WINDOW + 1:
            return False
        close = s["Close"]
        o = obv_ind(close, s["Volume"])
        return bool(slope(o, OBV_SLOPE_WINDOW) > 0 and slope(close, OBV_SLOPE_WINDOW) <= 0)
    except Exception:
        return False


def obv_bearish_divergence(s, b=None):
    """量價背離(出貨警示): price rising while OBV falling — strategy.py:140-141.
    True iff slope(close,20) > 0 and slope(obv,20) < 0."""
    try:
        if s is None or len(s) < OBV_SLOPE_WINDOW + 1:
            return False
        close = s["Close"]
        o = obv_ind(close, s["Volume"])
        return bool(slope(close, OBV_SLOPE_WINDOW) > 0 and slope(o, OBV_SLOPE_WINDOW) < 0)
    except Exception:
        return False


def adjudicate(m):
    """PASS/FAIL verdict for a bullish-signal metrics dict per the weighting-gate ruler.

    PASS iff (a) Wilson-CI-lower > base_rate  AND  (b) fired >= FIRED_FLOOR  AND
            (c) FLAT-regime lift > FLAT_BETA_THRESH (alpha, not market-beta).
    Pure function — no I/O. Returns the verdict + each condition for the report.
    """
    ci_lo = m["precision_ci"][0]
    base = m.get("base_rate", 0.0)
    fired = m.get("fired", 0)
    flat_lift = m.get("by_regime", {}).get("flat", {}).get("lift", 0.0)
    ci_beats_base = bool(ci_lo > base)
    fired_ok = bool(fired >= FIRED_FLOOR)
    flat_ok = bool(flat_lift > FLAT_BETA_THRESH)
    passed = ci_beats_base and fired_ok and flat_ok
    return {
        "verdict": "PASS" if passed else "FAIL",
        "ci_beats_base": ci_beats_base,
        "fired_ok": fired_ok,
        "flat_ok": flat_ok,
        "ci_lo": ci_lo, "base": base, "fired": fired, "flat_lift": flat_lift,
    }


def filter_benefit(m):
    """Bearish-as-AVOID-filter benefit: avg fwd AFTER the sell-warning vs the universe base.

    gap = avg_fwd_signaled - avg_fwd_all. A NEGATIVE gap means names that fire the
    出貨警示 underperform the universe over the next horizon → avoiding them adds value.
    None-safe when nothing fired. Pure overlay math — never enters scoring.
    """
    sig = m.get("avg_fwd_signaled")
    allm = m.get("avg_fwd_all")
    if sig is None or allm is None or not m.get("fired"):
        return {"gap": None, "avoiding_helps": False,
                "avg_fwd_signaled": sig, "avg_fwd_all": allm}
    gap = round(sig - allm, 2)
    return {"gap": gap, "avoiding_helps": bool(gap < 0.0),
            "avg_fwd_signaled": sig, "avg_fwd_all": allm}


def run_obv_adjudication(years=15, horizon=60, explosive=25.0, out_path="backtest_obv.txt"):
    """Backtest both OBV-divergence signals over `years`y and emit the adjudication file.

    Bullish → PASS/FAIL keep-the-weight verdict. Bearish → filter-benefit verdict.
    Returns {'bullish': {...}, 'bearish': {...}} with metrics + verdicts."""
    period = f"{years}y"
    tickers = BREADTH_TW + BREADTH_US + (BUSTED_PEERS if INCLUDE_BUSTED else [])
    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    emit(f"Downloading {len(tickers)} tickers x {period} "
         f"({len(BUSTED_PEERS) if INCLUDE_BUSTED else 0} busted-peer stress names) ...")
    hist = data_fetcher.get_universe(tickers, period=period)
    bench_raw = data_fetcher.get_universe(["^TWII", "^GSPC"], period=period)
    bench = {"twii": bench_raw.get("^TWII"), "sp500": bench_raw.get("^GSPC")}
    n_busted = sum(1 for t in BUSTED_PEERS if hist.get(t) is not None) if INCLUDE_BUSTED else 0
    emit(f"Got {len(hist)} histories ({n_busted} busted peers resolved). "
         f"Fills: next-open={NEXT_OPEN}, slippage={SLIP_BPS}bps, fee={FEE_BPS}bps (net-of-cost)")
    emit("")

    def bt(fn):
        return backtest.backtest_signal(
            hist, fn, bench_history=bench, horizon=horizon, step=10,
            explosive_pct=explosive, min_bars=200, next_open_fill=NEXT_OPEN,
            slippage_bps=SLIP_BPS, fee_bps=FEE_BPS)

    m_bull = bt(obv_bullish_divergence)
    m_bear = bt(obv_bearish_divergence)
    v_bull = adjudicate(m_bull)
    fb_bear = filter_benefit(m_bear)
    base_rate = m_bull.get("base_rate", 0.0)

    # ── DELIVERABLE TABLE ───────────────────────────────────────────────────────────────
    emit("=" * 96)
    emit("OBV VOLUME-PRICE DIVERGENCE — WEIGHTING-GATE ADJUDICATION (strategy.py:135-141)")
    emit("=" * 96)
    hdr = (f"{'signal':<26}{'fired':>6}{'prec':>8}{'lift':>6}{'CIlo':>8}{'CI>base':>9}"
           f"{'UP':>6}{'FLAT':>6}{'DOWN':>6}{'p50':>7}")
    emit(hdr)
    emit("-" * len(hdr))
    for label, m in (("量能流入(背離偏多,+10)", m_bull), ("量價背離(出貨警示,-15)", m_bear)):
        r = m["by_regime"]
        emit(f"{label:<26}{m['fired']:>6}{m['precision']:>8.2%}{m['lift']:>6.2f}"
             f"{m['precision_ci'][0]:>8.2%}{('Y' if m['ci_beats_base'] else 'n'):>9}"
             f"{r['up']['lift']:>6.2f}{r['flat']['lift']:>6.2f}{r['down']['lift']:>6.2f}"
             f"{(m['fwd_p50'] or 0):>6.1f}%")
    emit("")
    emit(f"base rate (P explosive >=+{explosive:.0f}%) = {base_rate:.2%}  "
         f"horizon={horizon}  family: 2 OBV-divergence signals")
    emit("")

    # ── BULLISH ADJUDICATION (the +10 live weight) ───────────────────────────────────────
    emit("=" * 96)
    emit("BULLISH 量能流入(背離偏多) — KEEP-THE-+10-WEIGHT ADJUDICATION")
    emit("-" * 96)
    emit(f"  fired={m_bull['fired']}  precision={m_bull['precision']:.2%}  "
         f"CI=[{m_bull['precision_ci'][0]:.2%},{m_bull['precision_ci'][1]:.2%}]  "
         f"base={base_rate:.2%}  lift={m_bull['lift']:.2f}")
    emit(f"    (a) CI-lo > base ({m_bull['precision_ci'][0]:.2%} > {base_rate:.2%}) = {v_bull['ci_beats_base']}")
    emit(f"    (b) fired >= {FIRED_FLOOR} ({m_bull['fired']}) = {v_bull['fired_ok']}")
    emit(f"    (c) FLAT-regime lift > {FLAT_BETA_THRESH:.1f} ({v_bull['flat_lift']:.2f}) = {v_bull['flat_ok']}")
    emit(f"  => VERDICT: {v_bull['verdict']}")
    emit("")

    # ── BEARISH ADJUDICATION (the -15 weight, as an AVOID filter) ────────────────────────
    emit("=" * 96)
    emit("BEARISH 量價背離(出貨警示) — AVOID-FILTER BENEFIT (the -15 weight)")
    emit("-" * 96)
    emit(f"  fired={m_bear['fired']}  precision(explosive)={m_bear['precision']:.2%}  "
         f"CI-lo={m_bear['precision_ci'][0]:.2%}  base={m_bear['base_rate']:.2%}")
    if fb_bear["gap"] is None:
        emit("  filter benefit: n/a (nothing fired)")
        bear_verdict = "FAIL"
    else:
        emit(f"  avg fwd AFTER warning = {fb_bear['avg_fwd_signaled']:.2f}%  vs  "
             f"universe base avg fwd = {fb_bear['avg_fwd_all']:.2f}%  "
             f"=> gap = {fb_bear['gap']:+.2f}%")
        if fb_bear["avoiding_helps"]:
            emit("  => names firing 出貨警示 UNDERPERFORM the universe over the next "
                 f"{horizon} bars — AVOIDING them adds value (filter benefit confirmed).")
            bear_verdict = "PASS"
        else:
            emit("  => names firing 出貨警示 do NOT underperform — the -15 penalty has no "
                 "demonstrated avoid-the-drop benefit (filter benefit NOT confirmed).")
            bear_verdict = "FAIL"
    emit(f"  => VERDICT: {bear_verdict}")
    emit("")

    # ── SUMMARY + RECOMMENDED ACTION ─────────────────────────────────────────────────────
    emit("=" * 96)
    emit("SUMMARY — ADJUDICATION & RECOMMENDED ACTION")
    emit("-" * 96)
    if v_bull["verdict"] == "PASS":
        emit("  量能流入(背離偏多,+10): PASS — CI-lo>base AND fired>=100 AND FLAT-lift>1.0.")
        emit("    ACTION: KEEP the +10 live weight at strategy.py:138-139. Commit THIS "
             "evidence file (backtest_obv.txt) as the missing weighting-gate proof.")
    else:
        fails = []
        if not v_bull["ci_beats_base"]:
            fails.append("CI-lo<=base (no edge over base rate)")
        if not v_bull["fired_ok"]:
            fails.append(f"fired<{FIRED_FLOOR} (n-thin, untrusted)")
        if not v_bull["flat_ok"]:
            fails.append("FLAT-lift<=1.0 (suspected market-beta, not alpha)")
        emit(f"  量能流入(背離偏多,+10): FAIL — {'; '.join(fails)}.")
        emit("    ACTION: DEMOTE to overlay-not-scorer. Recommended wiring (NOT applied here):")
        emit("      • strategy.py: DELETE the bullish branch lines 138-139 "
             "(if obv_s>0 and price_s<=0: factors[\"量能流入(背離偏多)\"]=10).")
        emit("      • web_export.py: surface 量能流入 as an INFORMATIONAL overlay badge "
             "(card-only, rides the same rail as A/D grade / earnings) — never summed into score.")
    emit("")
    if bear_verdict == "PASS":
        emit("  量價背離(出貨警示,-15): PASS — fired names underperform → the avoid filter works.")
        emit("    ACTION: KEEP the -15 live weight at strategy.py:140-141.")
    else:
        emit("  量價背離(出貨警示,-15): FAIL — no demonstrated avoid-the-drop benefit.")
        emit("    ACTION: DEMOTE to overlay-not-scorer. Recommended wiring (NOT applied here):")
        emit("      • strategy.py: DELETE the bearish branch lines 140-141 "
             "(elif price_s>0 and obv_s<0: factors[\"量價背離(出貨警示)\"]=-15).")
        emit("      • web_export.py: surface 量價背離 as an INFORMATIONAL warn-overlay badge "
             "(card-only) — never summed into score.")
    emit("")

    # ── PROVENANCE / HONESTY FOOTER (same disclosure family as backtest_15y_hardened.txt) ─
    emit(f"base: {base_rate:.4%}  horizon={horizon} (primary)  explosive=+{explosive:.0f}%  "
         f"family m=2 (bullish + bearish OBV divergence)")
    emit(f"coverage: {len(hist)} names ({n_busted} busted-peer stress names) · "
         f"net-of-cost (slip {SLIP_BPS}bps + fee {FEE_BPS}bps) · next-open fill · "
         f"INCLUDE_BUSTED={INCLUDE_BUSTED}")
    emit("look-ahead/survivorship: signal sees only df.iloc[:i+1] (backtest.py:306); "
         "next-open fill (backtest.py:138); BUSTED_PEERS mixed in. "
         "survivor-only universe — every lift is an OPTIMISTIC UPPER BOUND.")
    emit("NO live scorer touched: strategy.py:135-141 / config.LEAD_* unchanged "
         "(demote/keep is a separate user-gated decision driven by this evidence).")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n[written] {out_path}")

    return {
        "bullish": {"metrics": m_bull, "verdict": v_bull},
        "bearish": {"metrics": m_bear, "filter_benefit": fb_bear, "verdict": bear_verdict},
    }


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    horizon = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    explosive = float(sys.argv[3]) if len(sys.argv) > 3 else 25.0
    run_obv_adjudication(years, horizon, explosive)


if __name__ == "__main__":
    main()
