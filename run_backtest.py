# -*- coding: utf-8 -*-
"""Hardened walk-forward backtest — the weighting gate (credibility overhaul).

Run: python run_backtest.py [years] [horizon] [explosive_pct]
Default: 15y / 60-bar / +25%.

Reports, per signal: fired count, precision, base rate, lift, Wilson-CI lower bound,
whether CI-lower-bound > base rate (the real keep/kill test), regime-split lift
(UP/FLAT/DOWN market), and the forward-return median. Then bars-to-target (arrival
distribution) for the validated signals. Uses realistic fills (next-open + slippage).

Only signals whose CI lower bound clears the base rate AND hold up across regimes
deserve live score weight. (5y said VCP∧Stage2 lift 2.0; 15y says 1.34 — the gap
was the 2020-24 AI bull. This harness makes that visible.)

Backtests price/RS signals only — theme + 月營收 have no keyless history (informational).
Still survivorship-biased (yfinance survivors): every lift is an optimistic upper bound.
"""
import sys
import functools
import json
import os
import datetime
import logging

# CJK signal names (VCP 收縮 …) crash the default cp1252 Windows console on print.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# The 653×15y background run died after 37min CPU with stdout FULLY BUFFERED —
# zero progress lines, zero traceback in the redirected log. Every print in this
# module must flush so background/redirected runs are observable in real time.
print = functools.partial(print, flush=True)  # noqa: A001

import data_fetcher
import technical_setup as ts
import volume_signals as vs
import signals
import breakout_radar as br
import backtest
import factor_signals as fs
import universe
from config import BREADTH_TW, BREADTH_US, BUSTED_PEERS, ADV_SLIPPAGE

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

SLIP_BPS = 15.0          # ~0.15% each side (bid/ask + impact) — the flat fallback/floor
FEE_BPS = 30.0           # round-trip commission + TW transaction tax (net-of-cost, G9)
NEXT_OPEN = True         # fill at next open (signal fires on close) — no exec look-ahead (G4)
INCLUDE_BUSTED = True    # add boomed-then-busted peers to fight survivorship (G3)


def rs_pure(s, b, w=50):
    try:
        if s is None or b is None or len(s) <= w or len(b) <= w:
            return False
        n = min(len(s), len(b))
        import numpy as np
        rs = (s["Close"].iloc[-n:].to_numpy(float) / b["Close"].iloc[-n:].to_numpy(float))[-w:]
        return rs[-1] >= rs.max() - 1e-12
    except Exception:
        return False


DEFS = {
    "Trend Template":        lambda s, b: ts.trend_template(s)["pass"],
    "VCP 收縮":              lambda s, b: ts.vcp(s)["pass"],
    "Pocket pivot":          lambda s, b: ts.pocket_pivot(s),
    "Power pivot(放量突破)":  lambda s, b: ts.power_pivot(s),
    "首次新高(久盤後)":       lambda s, b: ts.first_new_high(s),
    "VDU→Thrust(量縮噴出)":   lambda s, b: vs.vdu_thrust(s),
    "U/D量比吸籌":            lambda s, b: vs.accumulating(s),
    "A/D吸籌A/B級":           lambda s, b: (vs.acc_dist_grade(s) or {}).get("bullish", False),
    "RS線新高(純)":           rs_pure,
    "VCP∧TrendTemplate":     lambda s, b: ts.vcp(s)["pass"] and ts.trend_template(s)["pass"],
    "RS純∧TrendTemplate":    lambda s, b: rs_pure(s, b) and ts.trend_template(s)["pass"],
    "PowerPivot∧TrendTmpl":  lambda s, b: ts.power_pivot(s) and ts.trend_template(s)["pass"],
    # factor_signals family — 12-1 momentum / SMA200 (REGISTERED-ONLY, not weighted;
    # candidates for the 15y factor run; b unused — pure OHLCV, graceful False).
    "Mom12-1>0":             lambda s, b: fs.mom_12_1_positive(s),
    "Close>SMA200":          lambda s, b: fs.above_sma200(s),
    "Mom12-1∧SMA200":        lambda s, b: fs.mom_with_sma200(s),
}


# ════════════════════════════════════════════════════════════════════════════
# sources/ OVERLAY signals — REGISTERED-ONLY, NOT WEIGHTED, GATED BY OVERLAY STATUS.
#
# The sources/ framework (twse/tpex/tdcc/sec) emits INFORMATIONAL overlays (chip /
# 法人 / 基本面 / 內部人). They are OVERLAY-NOT-SCORER today: NOTHING here enters the
# live scorer (strategy.py) or the weighted DEFS/EARLY_DEFS families above. We register
# their predicates in a SEPARATE dict so a FUTURE backtest can measure each one's edge
# BEFORE any weight is ever considered (the same Wilson-CI keep/kill gate the price
# signals went through). DO NOT add any of these to DEFS / EARLY_DEFS / config.LEAD_*.
#
# GATING: each predicate is "gated by overlay status" — it reads the overlay sidecar
# (chip/法人/基本面/內部人) the daily run attached to a card, NOT OHLCV. A card with no
# overlays of the relevant kind/label simply never fires the signal (graceful False),
# so the source being SKIPped that day produces no spurious fires. These predicates take
# a CARD dict (with an 'overlays' list), not the (s, b) OHLCV frames DEFS uses — they are
# registered for a future overlay-aware backtest harness, not the price harness. Pure.
# ════════════════════════════════════════════════════════════════════════════

def _overlay_has(card, *, kind=None, source=None, label_contains=None, severity=None):
    """True iff `card` carries an overlay matching ALL given filters. Reads the
    informational 'overlays' sidecar only — never any score/factor key. Pure; a
    card with no overlays (source SKIPped) → False (graceful, no spurious fire)."""
    for o in (card or {}).get("overlays", []) or []:
        if not isinstance(o, dict):
            continue
        if kind is not None and o.get("kind") != kind:
            continue
        if source is not None and str(o.get("source", "")) != source:
            continue
        if severity is not None and o.get("severity") != severity:
            continue
        if label_contains is not None and label_contains not in str(o.get("label", "")):
            continue
        return True
    return False


# ── P2 ENVIRONMENT-gated predicates (market/sector level, NOT per-stock) ───────────────
# The P2 market-level sources (taifex regime / macro_tw industry / macro_us macro) produce a
# single 'environment' dict of named gauges, NOT a per-card overlay. To register them in the
# SAME unweighted OVERLAY_DEFS family (so a FUTURE env-aware backtest can measure each gauge's
# regime-conditioning edge), each predicate reads a card's optional '_environment' sidecar
# (the env dict the daily run could attach for an env-aware harness). A card with no
# '_environment' (today's price-only harness) → False (graceful, no spurious fire). These are
# UNWEIGHTED, informational, gated-by-environment-status — NEVER added to DEFS/EARLY_DEFS/
# config.LEAD_* and NEVER read by strategy.py.
def _env_of(card):
    """The market-level environment dict a card may carry under '_environment', else {}.
    Pure read of an informational sidecar — never any score/factor key."""
    env = (card or {}).get("_environment")
    return env if isinstance(env, dict) else {}


def _env_regime_is(card, hint):
    """True iff the card's environment regime_hint == hint (risk_on/neutral/risk_off).
    Reads environment['regime']['regime_hint'] only. Graceful → False. Pure."""
    reg = _env_of(card).get("regime")
    return isinstance(reg, dict) and reg.get("regime_hint") == hint


def _env_cycle_light_in(card, lights):
    """True iff the 景氣對策信號 燈號 ∈ lights (e.g. {'紅','黃紅'}). Reads
    environment['industry']['business_cycle']['light']. Graceful → False. Pure."""
    ind = _env_of(card).get("industry")
    bc = ind.get("business_cycle") if isinstance(ind, dict) else None
    return isinstance(bc, dict) and bc.get("light") in lights


def _env_yoy_positive(card, section, key):
    """True iff environment[section][key] (a YoY fraction) is > 0. Graceful → False. Pure."""
    sec = _env_of(card).get(section)
    if not isinstance(sec, dict):
        return False
    v = sec.get(key)
    return isinstance(v, (int, float)) and v > 0


def _env_sector_tilt_is(card, sector, tilt):
    """True iff the P3 CFTC-COT environment['sector_tilt'][sector]['tilt'] == tilt
    (long/short/neutral). SECTOR/MARKET-level gauge, NOT per-stock — registered for a
    FUTURE env-aware backtest only. Reads the informational sidecar; graceful → False. Pure."""
    st = _env_of(card).get("sector_tilt")
    if not isinstance(st, dict):
        return False
    bucket = st.get(sector)
    return isinstance(bucket, dict) and bucket.get("tilt") == tilt


# Registered overlay-derived signal predicates. Signature is (card) → bool (NOT the
# (s, b) OHLCV signature of DEFS) — these are gated by attached overlay status, to be
# scored by a FUTURE overlay-aware backtest. UNWEIGHTED, informational, never live.
OVERLAY_DEFS = {
    # 三大法人買超 (TWSE T86 上市)
    "法人買超(T86,上市,overlay)":
        lambda card: _overlay_has(card, kind="inst", source="twse_t86", label_contains="買超"),
    # 上櫃三大法人同步買 (TPEx 3insti, warn = foreign∧trust both buying)
    "上櫃法人同買(TPEx,overlay)":
        lambda card: _overlay_has(card, kind="inst", source="tpex", severity="warn"),
    # 融資餘額單日暴增 (TWSE/TPEx margin surge — retail leverage warn)
    "融資暴增(margin,overlay)":
        lambda card: _overlay_has(card, kind="chip", label_contains="融資"),
    # 融券回補 (TWSE short cover — squeeze fuel easing)
    "融券回補(short-cover,overlay)":
        lambda card: _overlay_has(card, kind="chip", label_contains="融券"),
    # 大戶吸籌 (TDCC 集保戶股權分散 — rising concentration + falling holders)
    "大戶吸籌(TDCC,overlay)":
        lambda card: _overlay_has(card, kind="chip", source="tdcc", label_contains="吸籌"),
    # 散戶化/出貨 (TDCC — falling 大戶 concentration, warn)
    "散戶化(TDCC,overlay)":
        lambda card: _overlay_has(card, kind="chip", source="tdcc", label_contains="散戶化"),
    # 內部人買進 (SEC EDGAR Form-4 — open-market P cluster, US)
    "內部人買進(SEC-Form4,overlay)":
        lambda card: _overlay_has(card, kind="inst", source="sec_edgar", label_contains="買進"),
    # 內部人賣出 (SEC EDGAR Form-4 — open-market S, warn)
    "內部人賣出(SEC-Form4,overlay)":
        lambda card: _overlay_has(card, kind="inst", source="sec_edgar", label_contains="賣出"),

    # ── P2 ENVIRONMENT-gated (market/sector regime conditioning; UNWEIGHTED, never live) ──
    # TAIFEX index-level regime: foreign-TX-net + PCR rule-of-thumb (taifex.to_environment).
    "環境_風險偏多(TAIFEX,env)":
        lambda card: _env_regime_is(card, "risk_on"),
    "環境_風險偏空(TAIFEX,env)":
        lambda card: _env_regime_is(card, "risk_off"),
    # macro_tw 景氣對策信號 燈號 (NDC 6099): 紅/黃紅 = 景氣熱絡 backdrop.
    "環境_景氣熱絡(macro_tw,env)":
        lambda card: _env_cycle_light_in(card, ("紅", "黃紅")),
    # macro_tw 電子外銷訂單 YoY > 0 — leading semiconductor-demand tailwind backdrop.
    "環境_電子訂單擴張(macro_tw,env)":
        lambda card: _env_yoy_positive(card, "industry", "electronics_export_yoy"),
    # macro_us CPI YoY > 0 (always true in practice — a placeholder gauge for an env-aware
    # inflation-regime split; UNWEIGHTED, informational).
    "環境_通膨為正(macro_us,env)":
        lambda card: _env_yoy_positive(card, "macro", "cpi_yoy"),

    # ── P3 NEWS / CATALYST / SENTIMENT / ATTENTION / FLOWS (UNWEIGHTED, never live) ──────────
    # Same gated-by-overlay-status pattern: each predicate reads a card's attached overlay
    # sidecar (news/wiki/HN/FTD) or the env sidecar (COT). A card with no overlay of the
    # relevant source/kind → False (graceful, no spurious fire). HIGH anti-signal risk: high
    # buzz / negative news / persistent FTD often = already-moved / high-volume — these are
    # registered ONLY for a FUTURE overlay-aware Wilson-CI backtest, NEVER added to DEFS/
    # EARLY_DEFS/config.LEAD_* and NEVER read by strategy.py.
    # news_catalyst: any catalyst headline attached to the card (source='news').
    "新聞催化(news,overlay)":
        lambda card: _overlay_has(card, kind="catalyst", source="news"),
    # news_catalyst: a NEGATIVE-tone catalyst (classify_severity → 'warn'). Anti-signal.
    "負面新聞(news,overlay)":
        lambda card: _overlay_has(card, kind="catalyst", source="news", severity="warn"),
    # news_catalyst: multi-source buzz aggregate (the per-ticker sentiment overlay).
    "新聞聲量(news-buzz,overlay)":
        lambda card: _overlay_has(card, kind="sentiment", source="news"),
    # altdata: Wikipedia pageview attention spike (source='wikipedia_pageviews').
    "維基關注度(wiki,overlay)":
        lambda card: _overlay_has(card, kind="sentiment", source="wikipedia_pageviews"),
    # altdata: Hacker News discussion buzz (tech universe only; source='hackernews').
    "HN討論熱度(hn,overlay)":
        lambda card: _overlay_has(card, kind="sentiment", source="hackernews"),
    # sec_flows: persistent/elevated FTD settlement-pressure chip (source='sec_ftd', warn).
    "FTD交割失敗(sec-ftd,overlay)":
        lambda card: _overlay_has(card, kind="chip", source="sec_ftd", label_contains="FTD"),

    # ── P3 ENVIRONMENT-gated CFTC-COT sector tilt (market/sector level, NOT per-stock) ───────
    # managed-money net crowding tilt per future-mapped sector (energy/materials/precious_metals).
    # Reads environment['sector_tilt']; gated-by-environment-status; UNWEIGHTED, never live.
    "環境_能源偏多(COT,env)":
        lambda card: _env_sector_tilt_is(card, "energy", "long"),
    "環境_原物料偏多(COT,env)":
        lambda card: _env_sector_tilt_is(card, "materials", "long"),
    "環境_貴金屬偏多(COT,env)":
        lambda card: _env_sector_tilt_is(card, "precious_metals", "long"),
}


# ════════════════════════════════════════════════════════════════════════════
# REQ4 EARLY-BOARD pre-registered backtest (council-frozen spec).
#
# Tests the 正要起漲 (about-to-rise) family — every signal GATED by
# breakout_radar.not_extended (in_flat_base ∧ above_rising_ma50 ∧ ext≤0.10 ∧ RSI<75)
# on df.iloc[:i+1]. The CRITICAL credibility move: the keep/kill base rate is measured
# on the not_extended-GATED universe (NOT the global 6.99%), so the gate's selection
# effect is visible and lifts are honest. m = family size = 8 (Bonferroni denom).
#
# Does NOT touch the live scorer (config.LEAD_* / strategy.py) — this is the separate
# user-gated decision. Output is gitignored (backtest_early_board.txt). Every lift is an
# optimistic upper bound (survivor universe + busted-peer stress set).
# ════════════════════════════════════════════════════════════════════════════

# Radar inflection tells, reused by the composite (#7) and the readiness board (#8).
# Each is pure-OHLCV (± bench). rs_line_turn_up needs bench; the rest don't.
def _radar_tells(df, bench):
    tells = []
    if br.spring(df):
        tells.append("spring")
    if br.lps(df):
        tells.append("LPS")
    if br.squeeze_coil(df):
        tells.append("squeeze_coil")
    if br.episodic_pivot(df):
        tells.append("episodic_pivot")
    if bench is not None and br.rs_line_turn_up(df, bench):
        tells.append("rs_turn_up")
    return tells


# The full set of co-firing tells used to enforce "first_new_high never alone" (#1).
# A 2nd tell can be ANY other early tell in the family — technical, volume, RS, or radar.
def _other_tells_fire(df, bench):
    if ts.power_pivot(df):
        return True
    if ts.pocket_pivot(df):
        return True
    if vs.accumulating(df):
        return True
    if bench is not None and (br.rs_line_turn_up(df, bench) or signals.rs_line_new_high(df, bench)):
        return True
    if _radar_tells(df, bench):                       # spring/LPS/coil/episodic
        return True
    return False


# Composite radar-inflection tell collapsed to ONE (#7): spring ∨ LPS ∨ coil ∨ episodic.
# (rs_turn_up is registered separately as #4, so it is NOT folded into this OR.)
def _radar_inflection_composite(df, bench):
    return bool(br.spring(df) or br.lps(df) or br.squeeze_coil(df) or br.episodic_pivot(df))


def _tell_count_for_board(df, bench):
    """Tell count for the readiness board (#8) — same tell vocabulary as
    breakout_radar.readiness: spring, LPS, squeeze_coil, episodic_pivot, rs_turn_up."""
    return len(_radar_tells(df, bench))


# The pre-registered early-board family. Each entry is GATED by not_extended on the
# same slice the tell sees (df = df.iloc[:i+1] already inside backtest_signal). m = 8.
EARLY_DEFS = {
    # 1. 久盤後首次新高 — but NEVER alone: require a 2nd co-firing tell (n-thin guard, the
    #    first_new_high lift-2.44/n-47 trap). Gate ∧ first_new_high ∧ (any other tell).
    "首次新高+2nd(early)":
        lambda s, b: br.not_extended(s) and ts.first_new_high(s) and _other_tells_fire(s, b),
    # 2. 放量突破 power pivot, gated.
    "PowerPivot(early)":
        lambda s, b: br.not_extended(s) and ts.power_pivot(s),
    # 3. pocket pivot, gated.
    "PocketPivot(early)":
        lambda s, b: br.not_extended(s) and ts.pocket_pivot(s),
    # 4. RS線平盤翻揚 rs_line_turn_up (breakout_radar), gated. Needs bench.
    "RS翻揚(early)":
        lambda s, b: br.not_extended(s) and b is not None and br.rs_line_turn_up(s, b),
    # 5. U/D 量比吸籌 accumulation (volume_signals), gated.
    "UD吸籌(early)":
        lambda s, b: br.not_extended(s) and vs.accumulating(s),
    # 6. RS線新高 de-trapped (signals.py), gated. Needs bench.
    "RS線新高(early)":
        lambda s, b: br.not_extended(s) and b is not None and signals.rs_line_new_high(s, b),
    # 7. radar-inflection COMPOSITE (spring ∨ LPS ∨ squeeze_coil ∨ episodic_pivot) → ONE.
    "雷達拐點複合(early)":
        lambda s, b: br.not_extended(s) and _radar_inflection_composite(s, b),
    # 8. THE READINESS BOARD itself: not_extended ∧ (tell_count ≥ 2) — drives the banner.
    "起漲板就緒(board)":
        lambda s, b: br.not_extended(s) and _tell_count_for_board(s, b) >= 2,
}

# beta? flag threshold — a signal whose FLAT-regime lift ≤ this is suspected market-beta.
FLAT_BETA_THRESH = 1.0
# hard floor — fewer than this many fires → reported but flagged n-thin, INELIGIBLE for weight.
FIRED_FLOOR = 100
# EXTENDED early-opportunity backtest: cap on the broad eligible universe (dollar-volume-
# ranked) used as the backtest set. ~300 names × ~10y is heavy + Yahoo-429-prone; this is
# the FEASIBLE-scope ceiling (override via the CLI 5th arg). Busted peers are added on top.
OPP_BACKTEST_CAP = 300


def assemble_early_opp_universe(cap=OPP_BACKTEST_CAP):
    """Assemble the BROAD early-opportunity-eligible backtest universe.

    Pulls the dollar-volume-ranked eligible names from universe.opportunity_universe()
    (TWSE/TPEx open-data + US CSV + supply-chain anchors) — the actual POPULATION the
    early picks are drawn from — NOT universe.scan_opportunities() (today's already-popped
    leaders, which would be severe survivorship bias). Truncates the eligible set to `cap`
    (feasibility / Yahoo-429), then unions in config.BUSTED_PEERS (names that popped then
    died — a partial survivorship offset). Deduped, busted-first preserved.

    Network-safe: if opportunity_universe raises (TWSE/Yahoo down), degrades to the busted
    peers alone and logs — never aborts (mirrors universe.py's everything-is-wrapped rule).

    NOTE — UNIVERSE-SELECTION CAVEAT: membership is CURRENT-DAY (today's dollar-volume
    rank). Backtesting a signal over today's members embeds look-ahead universe-selection
    bias that CANNOT be reconstructed point-in-time keyless. Keeping BUSTED_PEERS in only
    partially offsets it. The runner states this caveat explicitly in its output.
    """
    eligible = []
    try:
        tickers, _names = universe.opportunity_universe()
        eligible = list(tickers)[: max(0, int(cap))]
    except Exception as e:                       # network down → busted-only, never abort
        logging.warning("SKIP opportunity_universe (busted-peers only): %s", e)
    merged, seen = [], set()
    for t in list(BUSTED_PEERS) + eligible:      # busted first so they always survive the cap
        if t not in seen:
            seen.add(t)
            merged.append(t)
    return merged


def run_early_board(years=15, horizon=60, explosive=25.0, out_path="backtest_early_board.txt",
                    extended=False, opp_cap=OPP_BACKTEST_CAP):
    """Run the REQ4 early-board family + emit the structured deliverable.

    Gate base rate is measured on the not_extended-GATED universe (the gate itself run
    as a pseudo-signal), and keep/kill (ci_beats_base + pvalue + Bonferroni + BH) is
    recomputed against THAT gated base — never the global 6.99%. Both bases reported.

    extended=True → backtest the SAME early-board family over the BROAD early-opportunity-
    ELIGIBLE universe (dollar-volume-ranked names from universe.opportunity_universe() via
    assemble_early_opp_universe), capped to opp_cap, instead of the narrow 82-name
    watchlist+busted set. Far more samples → tighter CIs + an edge estimate on the actual
    population the early picks are drawn from. The engine (point-in-time df.iloc[:i+1],
    next-open fill) and the multiple-testing correction_gate are UNCHANGED; only the test
    universe widens. The output adds an EXPLICIT current-day universe-selection caveat."""
    period = f"{years}y"
    if extended:
        tickers = assemble_early_opp_universe(cap=opp_cap)
    else:
        tickers = BREADTH_TW + BREADTH_US + (BUSTED_PEERS if INCLUDE_BUSTED else [])
    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    if extended:
        emit(f"[EXTENDED early-opportunity universe] backtesting the early-board family over "
             f"the BROAD dollar-volume-ranked eligible set (opportunity_universe, cap={opp_cap}) "
             f"+ {len(BUSTED_PEERS)} busted-peer stress names — NOT the popped-leaders list.")
    emit(f"Downloading {len(tickers)} tickers x {period} "
         f"({len(BUSTED_PEERS) if INCLUDE_BUSTED else 0} busted-peer stress names) ...")
    hist = data_fetcher.get_universe(tickers, period=period)
    bench_raw = data_fetcher.get_universe(["^TWII", "^GSPC"], period=period)
    bench = {"twii": bench_raw.get("^TWII"), "sp500": bench_raw.get("^GSPC")}
    n_busted = sum(1 for t in BUSTED_PEERS if hist.get(t) is not None) if INCLUDE_BUSTED else 0
    emit(f"Got {len(hist)} histories ({n_busted} busted peers resolved). "
         f"Fills: next-open={NEXT_OPEN}, slippage={SLIP_BPS}bps, fee={FEE_BPS}bps, "
         f"INCLUDE_BUSTED={INCLUDE_BUSTED}")
    emit("")

    bt = lambda fn: backtest.backtest_signal(
        hist, fn, bench_history=bench, horizon=horizon, step=10,
        explosive_pct=explosive, min_bars=200, next_open_fill=NEXT_OPEN,
        slippage_bps=SLIP_BPS, fee_bps=FEE_BPS)

    # ── GATED BASE RATE: run not_extended itself as a pseudo-signal. Its precision =
    #    P(explosive | not_extended fired) = the base rate on the gated universe. The
    #    global base rate is the same metric's base_rate field (unconditional). ─────────
    gate_m = bt(lambda s, b: br.not_extended(s))
    gated_base = gate_m["precision"]            # P(explosive | gate)
    global_base = gate_m["base_rate"]           # P(explosive) over all windows
    emit(f"GATED base rate (P explosive | not_extended) = {gated_base:.4%}  "
         f"[gate fired {gate_m['fired']} windows, {gate_m['fired_explosive']} explosive]")
    emit(f"GLOBAL base rate (P explosive, ungated)       = {global_base:.4%}")
    emit(f"gate selection effect: {gated_base - global_base:+.4%} "
         f"({'gate concentrates explosives' if gated_base > global_base else 'gate dilutes'})")
    emit("")

    # ── Run the 8-signal family. Override each result's base_rate to the GATED base,
    #    then recompute ci_beats_base (Wilson CI lower > gated base) BEFORE correction. ──
    results = []
    for name, fn in EARLY_DEFS.items():
        m = bt(fn)
        m["name"] = name
        m["global_base"] = global_base
        m["gated_base"] = gated_base
        m["base_rate"] = gated_base                              # gate keep/kill against GATED base
        ci_lo = m["precision_ci"][0]
        m["ci_beats_base"] = bool(ci_lo > gated_base)            # recompute vs gated base
        m["lift_gated"] = round((m["precision"] / gated_base), 3) if gated_base else 0.0
        results.append(m)

    # ── Multiple-testing correction over the FULL family (m = 8). correction_gate reads
    #    each result's (now gated) base_rate for pvalue + uses ci_beats_base we set. ─────
    gated = backtest.correction_gate(results, alpha=0.05, q=0.10)

    # ── hard floor (d): fired ≥ FIRED_FLOOR; below → n-thin, INELIGIBLE regardless of lift.
    for g in gated:
        thin = g["fired"] < FIRED_FLOOR
        g["n_thin"] = thin
        # kept(correction) AND passes hard floor → eligible for live weight
        g["kept_final"] = bool(g["kept"] and not thin)
        flat_lift = g["by_regime"]["flat"]["lift"]
        g["flat_regime_lift"] = flat_lift
        g["beta_flag"] = bool(flat_lift <= FLAT_BETA_THRESH)

    # ── DELIVERABLE TABLE ─────────────────────────────────────────────────────────────
    emit("=" * 118)
    emit("REQ4 EARLY-BOARD FAMILY (m=8, Bonferroni α/m, BH q=0.10) — keep/kill vs GATED base")
    emit("=" * 118)
    hdr = (f"{'signal':<22}{'fired':>6}{'prec':>8}{'CIlo':>8}{'gbase':>8}{'glob':>8}"
           f"{'liftG':>7}{'p':>9}{'Bonf':>5}{'BH':>4}{'CI>b':>5}{'n>=100':>7}"
           f"{'KEEP':>6}{'FLAT':>6}{'beta?':>6}")
    emit(hdr)
    emit("-" * len(hdr))
    for g in gated:
        emit(f"{g['name']:<22}{g['fired']:>6}{g['precision']:>8.2%}"
             f"{g['precision_ci'][0]:>8.2%}{g['gated_base']:>8.2%}{g['global_base']:>8.2%}"
             f"{g['lift_gated']:>7.2f}{g['pvalue']:>9.4f}"
             f"{('Y' if g['bonferroni_pass'] else 'n'):>5}"
             f"{('Y' if g['bh_pass'] else 'n'):>4}"
             f"{('Y' if g['ci_beats_base'] else 'n'):>5}"
             f"{('Y' if not g['n_thin'] else 'THIN'):>7}"
             f"{('YES' if g['kept_final'] else 'no'):>6}"
             f"{g['flat_regime_lift']:>6.2f}"
             f"{('Y' if g['beta_flag'] else 'n'):>6}")
    emit("")

    # ── PER-SIGNAL gate breakdown (a)(b)(c)(d) + regime split ──────────────────────────
    emit("PER-SIGNAL GATE BREAKDOWN  [(a) CI-lo>gated-base  (b) Bonferroni p<=0.05/8  "
         "(c) BH q=0.10  (d) fired>=100]")
    emit("-" * 118)
    for g in gated:
        r = g["by_regime"]
        emit(f"  {g['name']}")
        emit(f"    fired={g['fired']}  precision={g['precision']:.2%}  "
             f"CI=[{g['precision_ci'][0]:.2%},{g['precision_ci'][1]:.2%}]  "
             f"gated_base={g['gated_base']:.2%}  global_base={g['global_base']:.2%}")
        emit(f"    (a) ci_beats_base={g['ci_beats_base']}  "
             f"(b) bonferroni_pass={g['bonferroni_pass']} (p={g['pvalue']:.4f}, thr={0.05/len(gated):.5f})  "
             f"(c) bh_pass={g['bh_pass']}  (d) fired>=100={not g['n_thin']}")
        emit(f"    regime lift  UP={r['up']['lift']:.2f}  FLAT={r['flat']['lift']:.2f}  "
             f"DOWN={r['down']['lift']:.2f}   "
             f"{'[beta? FLAT-lift<=1.0 — suspected market-beta not alpha]' if g['beta_flag'] else ''}")
        verdict = ("KEPT — eligible for live weight" if g["kept_final"]
                   else ("KILLED — n-thin, untrusted, INELIGIBLE for weight" if g["n_thin"]
                         else "KILLED — failed correction gate"))
        emit(f"    => {verdict}")
        emit("")

    # ── KEPT SET ───────────────────────────────────────────────────────────────────────
    kept_final = [g for g in gated if g["kept_final"]]
    emit("=" * 118)
    emit("KEPT SET — passes ALL FOUR gate conditions (a CI>gated-base, b Bonferroni, "
         "c BH, d fired>=100):")
    if kept_final:
        for g in sorted(kept_final, key=lambda x: -x["lift_gated"]):
            warn = "  [beta? flat-lift<=1]" if g["beta_flag"] else ""
            emit(f"  {g['name']:<22} lift(gated) {g['lift_gated']:.2f}  "
                 f"fired={g['fired']}  flat-lift={g['flat_regime_lift']:.2f}{warn}")
    else:
        emit("  (none — no early-board signal cleared all four conditions)")
    emit("")

    # ── SHIP VERDICT ───────────────────────────────────────────────────────────────────
    board_sig = next((g for g in gated if g["name"] == "起漲板就緒(board)"), None)
    emit("SHIP VERDICT")
    emit("-" * 118)
    emit("Per-signal (does each early tell PASS the backtest, i.e. earn a 'validated' banner?):")
    for g in gated:
        emit(f"  {g['name']:<22} {'PASS — drop 未通過回測 banner' if g['kept_final'] else 'FAIL — show 未通過回測，純資訊 banner'}")
    emit("")
    if board_sig is not None:
        board_pass = board_sig["kept_final"]
        emit(f"BOARD-READINESS signal (#8 起漲板就緒 = not_extended ∧ tell_count>=2) — "
             f"this drives the BOARD'S banner:")
        emit(f"  fired={board_sig['fired']}  precision={board_sig['precision']:.2%}  "
             f"lift(gated)={board_sig['lift_gated']:.2f}  "
             f"CI>gated-base={board_sig['ci_beats_base']}  Bonferroni={board_sig['bonferroni_pass']}  "
             f"BH={board_sig['bh_pass']}  fired>=100={not board_sig['n_thin']}")
        emit(f"  => EARLY BOARD {'PASSES' if board_pass else 'FAILS'} the backtest → "
             f"{'DROP the banner (validated edge)' if board_pass else 'SHOW 未通過回測，純資訊 banner'}")
    else:
        emit("  BOARD signal not found (unexpected).")
    emit("")

    # ── FUTURE LIVE LEAD_* WEIGHT RECOMMENDATION (do NOT add — user sign-off only) ──────
    emit("FUTURE LIVE LEAD_* WEIGHT — RECOMMENDATION ONLY (NOT applied; needs user sign-off):")
    weight_candidates = [g for g in kept_final if not g["beta_flag"]]
    if weight_candidates:
        for g in sorted(weight_candidates, key=lambda x: -x["lift_gated"]):
            emit(f"  RECOMMEND {g['name']:<22} (gated lift {g['lift_gated']:.2f}, "
                 f"fired {g['fired']}, flat-lift {g['flat_regime_lift']:.2f}) — propose for a "
                 f"future LEAD_* weight after user sign-off. NOT added here.")
    else:
        beta_kept = [g for g in kept_final if g["beta_flag"]]
        if beta_kept:
            for g in beta_kept:
                emit(f"  HOLD {g['name']:<22} passed the gate but FLAT-lift<=1.0 (beta?) — "
                     f"do NOT weight (suspected market-beta, not alpha).")
        else:
            emit("  (none qualify — no early-board signal earns a live weight recommendation)")
    emit("")

    # ── PROVENANCE / HONESTY FOOTER ────────────────────────────────────────────────────
    emit(f"base: gated={gated_base:.4%}  global={global_base:.4%}  "
         f"horizon={horizon} (primary)  explosive=+{explosive:.0f}%  family m={len(gated)}")
    emit(f"coverage: {len(hist)} names ({n_busted} busted-peer stress names) · "
         f"net-of-cost (slip {SLIP_BPS}bps + fee {FEE_BPS}bps) · next-open fill · "
         f"INCLUDE_BUSTED={INCLUDE_BUSTED}")
    emit("look-ahead/survivorship: signal sees only df.iloc[:i+1] (backtest.py:306); "
         "next-open fill (backtest.py:138); BUSTED_PEERS mixed in. "
         "survivor-only universe — every lift is an OPTIMISTIC UPPER BOUND.")
    if extended:
        emit("UNIVERSE-SELECTION CAVEAT (extended run): the opportunity-eligible universe "
             "membership is CURRENT-DAY (today's dollar-volume rank from opportunity_universe). "
             "Backtesting a signal over today's members embeds look-ahead universe-selection "
             "bias — names that survived/grew enough to rank today are over-represented in "
             "history. This CANNOT be reconstructed point-in-time keyless (no historical "
             "dollar-volume membership snapshots). BUSTED_PEERS (popped-then-died) are kept in "
             "to PARTIALLY offset it, but the lift remains an OPTIMISTIC UPPER BOUND on top of "
             "the survivor-only-yfinance bias. Signal computation itself is still strictly "
             "point-in-time (df.iloc[:i+1]); only universe membership is current-day.")
    emit("NO live scorer touched: config.LEAD_* / strategy.py unchanged (separate user-gated decision).")

    # ── SECONDARY HORIZONS {20,120} — reported only, do NOT govern keep/kill ────────────
    emit("")
    emit("SECONDARY HORIZONS {20,120} (REPORTED ONLY — primary 60 governs keep/kill):")
    for sec_h in (20, 120):
        emit(f"  horizon={sec_h}:")
        sec_gate = backtest.backtest_signal(
            hist, lambda s, b: br.not_extended(s), bench_history=bench, horizon=sec_h,
            step=10, explosive_pct=explosive, min_bars=200, next_open_fill=NEXT_OPEN,
            slippage_bps=SLIP_BPS, fee_bps=FEE_BPS)
        sec_gbase = sec_gate["precision"]
        emit(f"    gated_base={sec_gbase:.2%} (global={sec_gate['base_rate']:.2%})")
        for name, fn in EARLY_DEFS.items():
            sm = backtest.backtest_signal(
                hist, fn, bench_history=bench, horizon=sec_h, step=10,
                explosive_pct=explosive, min_bars=200, next_open_fill=NEXT_OPEN,
                slippage_bps=SLIP_BPS, fee_bps=FEE_BPS)
            slift = round(sm["precision"] / sec_gbase, 2) if sec_gbase else 0.0
            emit(f"    {name:<22} fired={sm['fired']:>5}  prec={sm['precision']:>6.2%}  "
                 f"lift(gated)={slift:>5.2f}")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n[written] {out_path}")
    return gated, kept_final, board_sig


def write_kelly_state(metrics_by_signal, path="docs/data/_kelly_state.json"):
    """Offline writer for the B11 Kelly position-size GUIDANCE overlay.

    Dumps per-signal edge stats for MULTIPLE-TESTING SURVIVORS ONLY (B12: kept==True,
    i.e. ci_beats_base AND Bonferroni AND BH) — an overlay only gives a Kelly hint to
    signals that already passed the (now multiple-comparisons-aware) weighting gate.
    OVERLAY-NOT-SCORER: this artifact feeds an informational position CEILING in the PWA;
    it is NEVER read by strategy.score_stock or ranking. Run offline by this script (not
    the daily cron), so a plain datetime.date.today() asof is fine.

    Accepts either a {name: metrics} mapping OR a list of gated dicts (each carrying
    'name' + the B12 'kept' annotation). Both forms gate on the corrected 'kept' so
    _kelly_state.json signals == the corrected KEEP list.
    """
    if isinstance(metrics_by_signal, dict):
        items = list((metrics_by_signal or {}).items())
    else:
        items = [(m.get("name"), m) for m in (metrics_by_signal or [])]
    state = {"asof": datetime.date.today().isoformat()}
    for name, m in items:
        if not m or not m.get("kept"):
            continue
        state[name] = {
            "win_rate": m.get("win_rate"),
            "avg_win_pct": m.get("avg_win_pct"),
            "avg_loss_pct": m.get("avg_loss_pct"),
            "expectancy_pct": m.get("expectancy_pct"),
            "kelly_raw": m.get("kelly_raw"),
            "kelly_half": m.get("kelly_half"),
            "kelly_capped": m.get("kelly_capped"),
            "ci_beats_base": bool(m.get("ci_beats_base")),
            "kept": True,
            "pvalue": m.get("pvalue"),
            "bonferroni_pass": bool(m.get("bonferroni_pass")),
            "bh_pass": bool(m.get("bh_pass")),
            "fired": m.get("fired"),
        }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_universe_csv(path):
    """Read a universe CSV (header: ticker,market,name,source) → ordered, deduped
    ticker list. Used by the 15y factor run to consume universe_15y_draft.csv.

    Fail-fast at the boundary: a missing file raises (FileNotFoundError/OSError)
    and a CSV that parses to ZERO tickers raises ValueError — silently backtesting
    an empty universe would be a far worse failure mode than aborting."""
    import csv as _csv
    out, seen = [], set()
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in _csv.DictReader(f):
            t = (row.get("ticker") or "").strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    if not out:
        raise ValueError(f"universe csv has no usable 'ticker' rows: {path}")
    return out


def load_universe_meta(path):
    """C2: read {ticker: added_date|None} from a universe CSV's optional 'added_date' column.
    Absent column → every value None (point-in-time membership becomes a no-op, back-compat).
    Same fail-fast boundary as load_universe_csv (a missing file raises)."""
    import csv as _csv
    out = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in _csv.DictReader(f):
            t = (row.get("ticker") or "").strip()
            if t:
                out[t] = (row.get("added_date") or "").strip() or None
    return out


def apply_pit_membership(history, added_dates):
    """C2 point-in-time membership: drop each name's bars BEFORE its added_date so a backtest
    window only sees names that were in the universe at that date — removing the look-ahead
    universe-selection bias of scoring a late-added name across early history (partial
    survivorship fix; the delisting half stays a yfinance limitation). Names with no
    added_date, or frames without a DatetimeIndex, pass through unchanged. A name whose whole
    history precedes its added_date drops out. Returns a NEW dict; inputs are not mutated."""
    import pandas as pd
    out = {}
    for sym, df in (history or {}).items():
        if df is None:
            continue
        ad = (added_dates or {}).get(sym)
        if not ad or not isinstance(getattr(df, "index", None), pd.DatetimeIndex):
            out[sym] = df
            continue
        try:
            cut = pd.Timestamp(ad)
        except Exception:
            out[sym] = df
            continue
        kept = df[df.index >= cut]
        if len(kept):
            out[sym] = kept
    return out


def _extract_universe_arg(argv):
    """Pure: pull an optional --universe arg out of argv WITHOUT disturbing the
    existing positional [years horizon explosive] CLI. Supports both
    '--universe=path' and '--universe path'. Returns (path_or_None, rest_argv)."""
    rest, csv_path, i = [], None, 0
    argv = list(argv or [])
    while i < len(argv):
        a = argv[i]
        if a.startswith("--universe="):
            csv_path = a.split("=", 1)[1]
        elif a == "--universe" and i + 1 < len(argv):
            csv_path = argv[i + 1]
            i += 1
        else:
            rest.append(a)
        i += 1
    return csv_path, rest


def _extract_fresh_arg(argv):
    """Pure: pull the optional `--fresh` flag out of argv WITHOUT disturbing the
    positional [years horizon explosive] CLI. `--fresh` forces a full recompute
    (ignore any `_event_15y_partial.json`). Returns (is_fresh, rest_argv)."""
    argv = list(argv or [])
    rest = [a for a in argv if a != "--fresh"]
    return (len(rest) != len(argv)), rest


def _resume_plan(state, cur_meta, *, names, fresh):
    """Pure: decide, from a read partial `state`, the current run's `cur_meta`,
    the ordered signal `names`, and the `--fresh` flag, which signals reuse the
    partial's metrics vs which must recompute.

    META-MATCH GUARD (critical correctness): a partial is reusable ONLY when its
    meta's (period, universe_csv, n_names) ALL equal the current run's — otherwise
    it is a stale partial from a different universe/window and EVERY signal
    recomputes (never serve another universe's old numbers). `fresh=True` forces a
    full recompute regardless. Returns:
        {'meta_match': bool,
         'cached':    {name: metrics}  # reuse partial metrics, no backtest_signal
         'recompute': [name, ...]}     # ordered subset of `names` to compute
    """
    names = list(names or [])
    p_meta = (state or {}).get("meta") or {}
    signals = (state or {}).get("signals") or {}
    keys = ("period", "universe_csv", "n_names")
    meta_match = bool(p_meta) and all(p_meta.get(k) == cur_meta.get(k) for k in keys)
    if fresh or not meta_match:
        return {"meta_match": meta_match, "cached": {}, "recompute": names}
    cached = {n: signals[n] for n in names if n in signals}
    recompute = [n for n in names if n not in cached]
    return {"meta_match": True, "cached": cached, "recompute": recompute}


def assemble_main_universe(universe_csv=None):
    """Ticker assembly for the default weighting backtest.

    universe_csv=None → the EXISTING behavior, unchanged: BREADTH_TW + BREADTH_US
    (+ BUSTED_PEERS when INCLUDE_BUSTED). With a csv path → the csv tickers (e.g.
    universe_15y_draft.csv, 653 TW150+US503 names) with BUSTED_PEERS appended
    (deduped, csv order preserved) so the survivorship stress set always rides."""
    if not universe_csv:
        return BREADTH_TW + BREADTH_US + (BUSTED_PEERS if INCLUDE_BUSTED else [])
    tickers = load_universe_csv(universe_csv)
    if INCLUDE_BUSTED:
        seen = set(tickers)
        tickers = tickers + [b for b in BUSTED_PEERS if b not in seen]
    return tickers


# ════════════════════════════════════════════════════════════════════════════
# Cache-first history loading + sanitize (the --universe 15y run).
#
# The one-shot yf.download of 653×15y is exactly what killed the first run; the
# cache builder (build_ohlcv_cache.py) already persisted 652 frames under
# .cache/ohlcv_15y/. Loading goes cache-first, falls back to ONE batch network
# fetch for the misses only, and every admitted frame passes through
# backtest_portfolio.sanitize_ohlcv (TW limit-rule spike/level-shift repair, US
# spike-revert) — 0050.TW carries a non-backadjusted 2025 split level shift and
# ~44 single names have similar artifacts that would pollute the event study.
# Imports of backtest_portfolio/build_ohlcv_cache stay LOCAL (inside functions):
# no top-level cycle is possible even if those modules ever import run_backtest.
# ════════════════════════════════════════════════════════════════════════════

def _slice_years(df, years):
    """Last ~years of daily bars (bar-based: years×252) — tz-safe, no date math."""
    bars = max(1, int(years) * 252)
    return df.iloc[-bars:] if len(df) > bars else df


def load_universe_history(tickers, years, cache_dir=None, fetch_missing=None,
                          max_fix=None, progress_every=25):
    """Cache-first, sanitize-always history loader → (hist, stats).

    Per ticker: .cache/ohlcv_15y/<safe>.pkl via build_ohlcv_cache.load_df; cache
    MISS → collected and fetched in ONE fallback batch (injectable fetch_missing
    for tests; default = data_fetcher.get_universe over the misses only). Every
    admitted frame is sliced to `years` then cleaned by sanitize_ohlcv with the
    market inferred from the suffix (.TW → TW limit rules, else US). Tickers
    needing > max_fix repairs are DROPPED (recorded, never silently kept);
    misses that fetch empty (delisted, e.g. NKLA) are recorded in skipped.
    Progress prints every `progress_every` names (flush=True module-wide).
    """
    import build_ohlcv_cache as boc            # local: keep import graph acyclic
    import backtest_portfolio as bp            # local: sanitize_ohlcv / MAX_FIXED_BARS
    cache_dir = cache_dir or boc.CACHE_DIR
    max_fix = bp.MAX_FIXED_BARS if max_fix is None else max_fix

    hist, fixed_log, dropped, skipped, missing = {}, {}, [], [], []
    n_cache = n_fetched = 0

    def _admit(t, df):
        df = _slice_years(df, years)
        market = "TW" if t.endswith(".TW") else "US"
        clean, fixed = bp.sanitize_ohlcv(df, market, max_fix=max_fix)
        if len(fixed) > max_fix:
            dropped.append(t)
            logging.warning("SKIP %s: %d repairs > max_fix=%d (dropped)",
                            t, len(fixed), max_fix)
            return False
        if fixed:
            fixed_log[t] = fixed
        hist[t] = clean
        return True

    n = len(tickers)
    for idx, t in enumerate(tickers, 1):
        df = boc.load_df(t, cache_dir)
        if df is None or getattr(df, "empty", True):
            missing.append(t)
        elif _admit(t, df):
            n_cache += 1
        if progress_every and (idx % progress_every == 0 or idx == n):
            print(f"[load] {idx}/{n} cache={n_cache} miss={len(missing)} "
                  f"dropped={len(dropped)}")

    if missing:
        fetch = fetch_missing or (
            lambda ts: data_fetcher.get_universe(ts, period=f"{int(years)}y"))
        try:
            fetched = fetch(missing) or {}
        except Exception as e:                 # network down → record, never abort
            logging.warning("SKIP fallback fetch (%d misses): %s", len(missing), e)
            fetched = {}
        for t in missing:
            df = fetched.get(t)
            if df is None or getattr(df, "empty", True):
                skipped.append(t)
                logging.warning("SKIP %s: cache miss + fetch empty (delisted?)", t)
            elif _admit(t, df):
                n_fetched += 1

    stats = {"n_cache": n_cache, "n_fetched": n_fetched,
             "skipped": skipped, "dropped": dropped, "fixed": fixed_log}
    return hist, stats


def _load_bench_cached(years, cache_dir=None, fetch_missing=None):
    """Benchmarks (^TWII/^GSPC) cache-first → {'twii': df|None, 'sp500': df|None}.

    Index levels carry no TW limit rules / split artifacts the stock sanitizer
    targets, so benches are NOT sanitized — sliced to `years` only. A bench that
    is neither cached nor fetchable comes back None (backtest_signal handles a
    None bench gracefully) — recorded, never raises."""
    import build_ohlcv_cache as boc
    cache_dir = cache_dir or boc.CACHE_DIR
    out = {}
    missing = []
    for sym in ("^TWII", "^GSPC"):
        df = boc.load_df(sym, cache_dir)
        out[sym] = None if df is None or getattr(df, "empty", True) else df
        if out[sym] is None:
            missing.append(sym)
    if missing:
        fetch = fetch_missing or (
            lambda ts: data_fetcher.get_universe(ts, period=f"{int(years)}y"))
        try:
            fetched = fetch(missing) or {}
        except Exception as e:
            logging.warning("SKIP bench fetch %s: %s", missing, e)
            fetched = {}
        for sym in missing:
            df = fetched.get(sym)
            if df is None or getattr(df, "empty", True):
                logging.warning("SKIP bench %s: not cached, not fetchable", sym)
            else:
                out[sym] = df
    return {"twii": _slice_years(out["^TWII"], years) if out["^TWII"] is not None else None,
            "sp500": _slice_years(out["^GSPC"], years) if out["^GSPC"] is not None else None}


# ════════════════════════════════════════════════════════════════════════════
# Incremental per-signal flush (crash-safe partial results).
# ════════════════════════════════════════════════════════════════════════════

PARTIAL_PATH = "_event_15y_partial.json"


def _jsonable(v):
    """Recursively coerce metrics into JSON-safe primitives (numpy scalars →
    python, tuples → lists). Unknown leaf types fall back to str — the partial
    file must NEVER abort the run."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, bool) or v is None or isinstance(v, (int, float, str)):
        return v
    try:
        import numpy as _np
        if isinstance(v, _np.integer):
            return int(v)
        if isinstance(v, _np.floating):
            return float(v)
    except Exception:
        pass
    return str(v)


def _atomic_write_json(path, state):
    """temp → os.replace: a mid-run kill never leaves a torn/half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read_partial(path):
    """Existing partial state, or {} on absent/corrupt (a torn file from a prior
    crash must not abort the new run — SKIP, log, restart fresh)."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning("SKIP corrupt partial %s (restart fresh): %s", path, e)
        return {}


def append_partial_result(path, signal_name, metrics, meta=None):
    """Flush ONE signal's metrics into the incremental partial file (atomic).

    Called right after each backtest_signal returns, so a process death mid-run
    still leaves every completed signal's metrics on disk and usable."""
    state = _read_partial(path)
    if meta:
        state["meta"] = _jsonable(meta)
    state.setdefault("signals", {})[str(signal_name)] = _jsonable(metrics)
    state["done"] = False
    state["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    _atomic_write_json(path, state)


def finalize_partial(path, gated):
    """Mark the partial file complete + attach the multiple-testing-gated list."""
    state = _read_partial(path)
    state["gated"] = _jsonable(list(gated or []))
    state["done"] = True
    state["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    _atomic_write_json(path, state)


def main(universe_csv=None, fresh=False):
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    horizon = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    explosive = float(sys.argv[3]) if len(sys.argv) > 3 else 25.0
    period = f"{years}y"

    tickers = assemble_main_universe(universe_csv)
    if universe_csv:
        # cache-first (.cache/ohlcv_15y) + sanitize — the 653×15y one-shot
        # yf.download is exactly what hung/killed the first background run.
        print(f"[universe csv] {universe_csv} → {len(tickers)} tickers "
              f"(busted peers appended: {INCLUDE_BUSTED}); cache-first + sanitize")
        hist, lstats = load_universe_history(tickers, years)
        print(f"[load done] hist={len(hist)} cache={lstats['n_cache']} "
              f"fetched={lstats['n_fetched']} dropped={lstats['dropped']} "
              f"skipped={lstats['skipped']} repaired={len(lstats['fixed'])}")
        # C2: point-in-time membership when the CSV carries added_date (no-op otherwise).
        added = load_universe_meta(universe_csv)
        if any(added.values()):
            before = len(hist)
            hist = apply_pit_membership(hist, added)
            print(f"[PIT] point-in-time membership applied "
                  f"({sum(1 for v in added.values() if v)} dated names; {before}→{len(hist)})")
        bench = _load_bench_cached(years)
    else:
        print(f"Downloading {len(tickers)} tickers x {period} "
              f"({len(BUSTED_PEERS) if INCLUDE_BUSTED else 0} busted-peer stress names) ...")
        hist = data_fetcher.get_universe(tickers, period=period)
        bench_raw = data_fetcher.get_universe(["^TWII", "^GSPC"], period=period)
        bench = {"twii": bench_raw.get("^TWII"), "sp500": bench_raw.get("^GSPC")}
    n_busted = sum(1 for t in BUSTED_PEERS if hist.get(t) is not None) if INCLUDE_BUSTED else 0
    _slip_desc = ("ADV-scaled (base/k/cap per config)" if ADV_SLIPPAGE else f"{SLIP_BPS}bps flat")
    print(f"Got {len(hist)} histories ({n_busted} busted peers resolved). "
          f"Fills: next-open={NEXT_OPEN}, slippage={_slip_desc}, fee={FEE_BPS}bps (net-of-cost)\n")

    hdr = f"{'signal':<22}{'fired':>6}{'prec':>7}{'lift':>6}{'CIlo':>7}{'CI>base':>8}" \
          f"{'p':>9}{'Bonf':>6}{'BH':>5}{'KEEP':>6}" \
          f"{'UP':>6}{'FLAT':>6}{'DOWN':>6}{'p50':>7}"
    print(hdr); print("-" * len(hdr))
    base_rate = None
    # B12: COLLECT all signal metrics first, then run the multiple-testing correction
    # over the FULL family before deciding keep/kill — a per-signal inline decision
    # cannot be Bonferroni/BH-aware (the family size isn't known until every signal ran).
    results = []
    partial_meta = {"asof": datetime.date.today().isoformat(), "period": period,
                    "horizon": horizon, "explosive_pct": explosive,
                    "universe_csv": universe_csv, "n_names": len(hist)}
    # IDEMPOTENT RESUME: a mid-run death (e.g. rate-limit at signal 9/15) left every
    # finished signal's metrics in PARTIAL_PATH. Reuse them so a restart only recomputes
    # the missing tail — but ONLY if the partial's meta matches THIS run's
    # (period, universe_csv, n_names); a stale partial from another universe is fully
    # recomputed. `--fresh` (fresh=True) forces a full recompute regardless.
    plan = _resume_plan(_read_partial(PARTIAL_PATH), partial_meta,
                        names=list(DEFS.keys()), fresh=fresh)
    if plan["cached"]:
        print(f"[resume] partial meta matches — reusing {len(plan['cached'])} cached "
              f"signal(s), recomputing {len(plan['recompute'])} "
              f"(--fresh to force full recompute)")
    elif fresh:
        print("[resume] --fresh → ignoring partial, recomputing all "
              f"{len(DEFS)} signals")
    for sig_i, (name, fn) in enumerate(DEFS.items(), 1):
        if name in plan["cached"]:
            # CACHED: reuse the partial's metrics — DO NOT call backtest_signal.
            m = dict(plan["cached"][name])
            m["name"] = name
            results.append(m)
            if m.get("base_rate") is not None:
                base_rate = m["base_rate"]   # full-universe base rate (signal-invariant)
            print(f"[signal {sig_i}/{len(DEFS)}] {name} (cached from partial)")
            continue
        m = backtest.backtest_signal(hist, fn, bench_history=bench, horizon=horizon,
                                     step=10, explosive_pct=explosive, min_bars=200,
                                     next_open_fill=NEXT_OPEN, slippage_bps=SLIP_BPS,
                                     fee_bps=FEE_BPS, adv_slippage=ADV_SLIPPAGE)
        m["name"] = name
        results.append(m)
        base_rate = m["base_rate"]
        # incremental flush: a mid-run death keeps every finished signal usable.
        append_partial_result(PARTIAL_PATH, name, m, meta=partial_meta)
        print(f"[signal {sig_i}/{len(DEFS)}] {name} fired={m['fired']} "
              f"prec={m['precision']:.2%}")

    # B12 multiple-testing correction over the full family (Bonferroni α/m + BH q=0.10).
    # gated[] are COPIES annotated with pvalue/bonferroni_pass/bh_pass/kept/family_size.
    gated = backtest.correction_gate(results, alpha=0.05, q=0.10)
    finalize_partial(PARTIAL_PATH, gated)

    keep = []
    for g in gated:
        r = g["by_regime"]
        flag = "YES" if g["kept"] else "no"
        print(f"{g['name']:<22}{g['fired']:>6}{g['precision']:>7.2%}{g['lift']:>6.2f}"
              f"{g['precision_ci'][0]:>7.2%}"
              f"{('Y' if g['ci_beats_base'] else 'n'):>8}"
              f"{g['pvalue']:>9.4f}"
              f"{('Y' if g['bonferroni_pass'] else 'n'):>6}"
              f"{('Y' if g['bh_pass'] else 'n'):>5}"
              f"{flag:>6}"
              f"{r['up']['lift']:>6.2f}{r['flat']['lift']:>6.2f}{r['down']['lift']:>6.2f}"
              f"{(g['fwd_p50'] or 0):>6.1f}%")
        if g["kept"]:
            keep.append((g["name"], g["lift"], r["flat"]["lift"]))

    # B11/B12 overlay: persist edge stats for the corrected KEEP list (multiple-testing
    # survivors only) → position-size CEILING. Gated on g['kept'], not raw ci_beats_base,
    # so _kelly_state.json signals == the corrected KEEP list above.
    try:
        from config import KELLY_STATE
        kelly_path = KELLY_STATE
    except Exception:
        kelly_path = "docs/data/_kelly_state.json"
    write_kelly_state(gated, kelly_path)

    print(f"\nbase rate={base_rate:.2%}  horizon={horizon}  explosive=+{explosive:.0f}%")
    print(f"coverage: {len(hist)} names ({n_busted} busted-peer stress names) · "
          f"net-of-cost (slip {SLIP_BPS}bps + fee {FEE_BPS}bps) · next-open fill")
    print("survivorship: partial — busted peers still trade; true delisted names "
          "are absent (yfinance survivors). Lift remains an optimistic upper bound.")
    print("\nKEEP (CI>base AND Bonferroni α/m AND BH q=0.10) — multiple-testing-corrected"
          " — eligible for live weight:")
    for name, lift, flat in sorted(keep, key=lambda x: -x[1]):
        beta_warn = "  [beta? flat-lift<=1]" if flat <= 1.0 else ""
        print(f"  {name:<22} lift {lift:.2f} (flat-regime {flat:.2f}){beta_warn}")

    print("\nARRIVAL TIME (bars-to-+%.0f%%, capped %d) — the honest 'when':" % (explosive, horizon * 2))
    for name in ["Trend Template", "Pocket pivot", "Power pivot(放量突破)", "RS純∧TrendTemplate"]:
        if name not in DEFS:
            continue   # arrival-time list is fixed; skip names absent from the active DEFS family
        bt = backtest.bars_to_target(hist, DEFS[name], bench_history=bench,
                                     max_horizon=horizon * 2, step=10,
                                     explosive_pct=explosive, min_bars=200)
        if bt["median_bars"] is not None:
            print(f"  {name:<22} median {bt['median_bars']:.0f} bars "
                  f"(IQR {bt['iqr_lo']:.0f}-{bt['iqr_hi']:.0f}), "
                  f"never-hit {bt['never_rate']:.0%}")


if __name__ == "__main__":
    # `python run_backtest.py early_board [years] [horizon] [explosive]` → REQ4 early board
    #   (narrow 82-name watchlist+busted set).
    # `python run_backtest.py early_board_opp [years] [horizon] [explosive] [cap]` → the SAME
    #   early-board family over the BROAD early-opportunity-eligible universe (dollar-volume-
    #   ranked, cap names + busted peers). Writes backtest_early_board_opp.txt.
    # Optional anywhere: `--universe path.csv` (or --universe=path.csv) → run the default
    #   weighting backtest over a CSV universe (e.g. universe_15y_draft.csv). Extracted
    #   BEFORE positional parsing so the existing [years horizon explosive] CLI is intact.
    # Anything else → the existing default leadership-weighting backtest (unchanged).
    _universe_csv, sys.argv = _extract_universe_arg(sys.argv)
    _fresh, sys.argv = _extract_fresh_arg(sys.argv)   # --fresh → ignore partial, full recompute
    if len(sys.argv) > 1 and sys.argv[1] == "early_board_opp":
        eb_years = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        eb_horizon = int(sys.argv[3]) if len(sys.argv) > 3 else 60
        eb_explosive = float(sys.argv[4]) if len(sys.argv) > 4 else 25.0
        eb_cap = int(sys.argv[5]) if len(sys.argv) > 5 else OPP_BACKTEST_CAP
        run_early_board(eb_years, eb_horizon, eb_explosive,
                        out_path="backtest_early_board_opp.txt",
                        extended=True, opp_cap=eb_cap)
    elif len(sys.argv) > 1 and sys.argv[1] == "early_board":
        eb_years = int(sys.argv[2]) if len(sys.argv) > 2 else 15
        eb_horizon = int(sys.argv[3]) if len(sys.argv) > 3 else 60
        eb_explosive = float(sys.argv[4]) if len(sys.argv) > 4 else 25.0
        run_early_board(eb_years, eb_horizon, eb_explosive)
    else:
        main(universe_csv=_universe_csv, fresh=_fresh)
