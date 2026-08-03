# -*- coding: utf-8 -*-
"""Stock scoring engine. Pure functions over OHLCV DataFrames + optional
institutional data + optional benchmark (for relative strength).

Factors: trend, momentum, volume, volatility, sector, institutional (liquidity-
gated), relative-strength-vs-index, 52-week-high proximity, RSI-14 (replaces the
old crude overheat rule), OBV volume-price divergence.
"""
import logging

import config
import signal_registry
from config import (SECTOR_MAP, SECTOR_WEIGHTS, STOCK_NAMES, VOLATILITY_CAP, MIN_BARS,
                    RS_WINDOW, RS_STRONG, HIGH_WINDOW, NEAR_HIGH, NEAR_MID, FAR_HIGH,
                    RSI_WINDOW, RSI_OVERBOUGHT, RSI_OVERSOLD,
                    INST_RATIO_FULL, INST_RATIO_HALF,
                    CONC_HIGH, CONC_MID, STREAK_MIN,
                    LEADERSHIP_WEIGHT, FACTOR_PTS)
# NOTE: BUCKET_SCORING / BUCKET_CAPS / BUCKET_IC_WEIGHTS are read via `config.` at call-time
# (NOT from-imported) so the A4a flag flip is live + testable — a bound bool wouldn't update.
# LEAD_* weights + the leadership predicates now live in signal_registry (B1, read via config.*).
from indicators import rsi as rsi_ind, obv as obv_ind, slope
from technical_setup import analyze_setup

log = logging.getLogger(__name__)

# The 52-week-high momentum premium needs most of a year of history to be meaningful — below
# this, `hi` is just a recent high and the +20/+10 bonus would be unearned (and mislabel a short
# high as 「接近52週高」). Gate the positive branches on it; the far_high penalty is unaffected.
HIGH_MIN_BARS = int(HIGH_WINDOW * 0.8)


# Factor → bucket classifier (de-collinearization). Rules are ordered; first match
# wins. Each scored factor label maps to exactly one orthogonal bucket.
_BUCKET_RULES = [
    ("meanrev", ("RSI", "遠離52週高")),
    ("relstr", ("相對強", "相對弱", "RS線新高")),
    ("trend", ("趨勢", "動能", "接近52週高", "逼近52週高", "Stage2", "久盤後首次新高")),
    ("volacc", ("量能", "波動穩定", "量價背離", "籌碼", "連買", "Pocket", "U/D量", "Power pivot", "VDU")),
    ("fund", ("產業", "外資", "投信")),
]


def _bucket_of(label):
    for bucket, keys in _BUCKET_RULES:
        if any(k in label for k in keys):
            return bucket
    return "fund"


def _bucket_score(factors):
    """Group factors into buckets, clamp each to ±BUCKET_CAPS (so the over-counted
    trend factor can't dominate), then IC-weight and sum. Returns (score, subtotals).
    Reads config.BUCKET_CAPS / config.BUCKET_IC_WEIGHTS dynamically so an offline-tuned
    weight set takes effect without re-import. RE-AGGREGATION ONLY — the input `factors`
    dict is never mutated (overlay-not-scorer / no-new-signal invariant)."""
    buckets = {}
    for label, pts in factors.items():
        buckets[_bucket_of(label)] = buckets.get(_bucket_of(label), 0) + pts
    capped = {}
    for b, v in buckets.items():
        cap = config.BUCKET_CAPS.get(b, 999)
        capped[b] = max(-cap, min(cap, v))
    score = int(round(sum(config.BUCKET_IC_WEIGHTS.get(b, 1.0) * v for b, v in capped.items())))
    return score, capped


def ic_gate_factor_pts(per_factor_ic, ic_min, base=None):
    """A5: derive a FACTOR_PTS override that DEMOTES (zeros) every base factor whose offline
    cross-sectional rank-IC is below ic_min. Pure — returns a NEW dict; the caller decides
    whether to apply it to config.FACTOR_PTS (gated, reversible). A factor with no IC entry
    keeps its weight (untested ≠ demoted). This is the base-factor analogue of the leadership
    CI gate, using cross-sectional IC because momentum-style base factors fail as daily
    event signals (the gap-d framework mismatch)."""
    base = dict(config.FACTOR_PTS if base is None else base)
    for key, ic in (per_factor_ic or {}).items():
        if key in base and ic is not None and ic < ic_min:
            base[key] = 0
    return base


def _rs_excess(df, bench, window):
    """Stock return minus benchmark return over `window` bars, or None."""
    try:
        s, b = df["Close"], bench["Close"]
        if len(s) <= window or len(b) <= window:
            return None
        s_ret = s.iloc[-1] / s.iloc[-1 - window] - 1
        b_ret = b.iloc[-1] / b.iloc[-1 - window] - 1
        return s_ret - b_ret
    except Exception:
        return None


def score_stock(df, sector=None, institutional=None, bench=None, chips=None):
    """Score one stock. Returns {score, factors, insufficient}."""
    if df is None or len(df) < MIN_BARS:
        return {"score": 0, "factors": {}, "insufficient": True}

    close, vol = df["Close"], df["Volume"]
    factors = {}

    # ── base factors ────────────────────────────────────────
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    up_trend = ma5.iloc[-1] > ma20.iloc[-1]
    # Base-factor weights come from config.FACTOR_PTS (B4) so the A5 IC gate can demote a
    # factor by zeroing its weight; `if pts:` means a 0-weight factor is never added.
    if up_trend and FACTOR_PTS["trend"]:
        factors["趨勢(MA5>MA20)"] = FACTOR_PTS["trend"]
    if close.iloc[-1] > close.iloc[-5] and FACTOR_PTS["momentum"]:
        factors["動能(5日上漲)"] = FACTOR_PTS["momentum"]
    ma20v = vol.rolling(20).mean().iloc[-1]
    if ma20v and vol.iloc[-1] > ma20v and FACTOR_PTS["volume"]:
        factors["量能(高於20日均量)"] = FACTOR_PTS["volume"]
    if close.pct_change().std() < VOLATILITY_CAP and FACTOR_PTS["vol_stable"]:
        factors["波動穩定"] = FACTOR_PTS["vol_stable"]
    if sector and SECTOR_WEIGHTS.get(sector):
        factors[f"產業({sector})"] = SECTOR_WEIGHTS[sector]

    # ── institutional, liquidity-gated (法人買超佔量比) ──────
    if institutional:
        foreign = institutional.get("foreign", 0) or 0
        trust = institutional.get("trust", 0) or 0
        ratio = abs(foreign) / ma20v if ma20v else 0
        mult = 1.0 if ratio >= INST_RATIO_FULL else (0.5 if ratio >= INST_RATIO_HALF else 0.0)
        if foreign > 0 and mult and FACTOR_PTS["inst_foreign_buy"]:
            factors["外資買超"] = round(FACTOR_PTS["inst_foreign_buy"] * mult)   # round, not int-truncate
        elif foreign < 0 and mult and FACTOR_PTS["inst_foreign_sell"]:
            factors["外資賣超"] = round(FACTOR_PTS["inst_foreign_sell"] * mult)
        if trust > 0 and FACTOR_PTS["inst_trust_buy"]:
            factors["投信買超"] = FACTOR_PTS["inst_trust_buy"]

    # ── relative strength vs index ──────────────────────────
    if bench is not None:
        rsx = _rs_excess(df, bench, RS_WINDOW)
        if rsx is not None:
            if rsx > RS_STRONG and FACTOR_PTS["rs_strong"]:
                factors["相對強弱(強於大盤)"] = FACTOR_PTS["rs_strong"]
            elif 0 < rsx <= RS_STRONG and FACTOR_PTS["rs_mild"]:
                factors["相對強弱(優於大盤)"] = FACTOR_PTS["rs_mild"]
            elif rsx <= 0 and FACTOR_PTS["rs_weak"]:
                factors["相對弱勢(弱於大盤)"] = FACTOR_PTS["rs_weak"]

    # ── 52-week-high proximity (George & Hwang 2004) ────────
    win = min(HIGH_WINDOW, len(df))
    hi = df["High"].rolling(win).max().iloc[-1]
    if hi and hi > 0:
        near = close.iloc[-1] / hi
        # The 52-week-high momentum premium is only meaningful with enough history. On a 20-30
        # bar cold-start frame `hi` is just a 1-month high, so don't award the +20/+10 bonus (nor
        # mislabel a 1-month high as 「接近52週高」). The far_high PENALTY still applies — a real
        # downtrend is bearish regardless of window length.
        enough = len(df) >= HIGH_MIN_BARS
        if enough and near >= NEAR_HIGH and FACTOR_PTS["near_high"]:
            factors["接近52週高"] = FACTOR_PTS["near_high"]
        elif enough and NEAR_MID <= near < NEAR_HIGH and FACTOR_PTS["near_mid"]:
            factors["逼近52週高"] = FACTOR_PTS["near_mid"]
        elif near < FAR_HIGH and FACTOR_PTS["far_high"]:
            factors["遠離52週高"] = FACTOR_PTS["far_high"]

    # ── RSI-14 (replaces old >30%-gain overheat rule) ───────
    r = rsi_ind(close, RSI_WINDOW)
    if r > RSI_OVERBOUGHT and FACTOR_PTS["rsi_overbought"]:
        factors["RSI過熱(>75)"] = FACTOR_PTS["rsi_overbought"]
    elif r < RSI_OVERSOLD and up_trend and FACTOR_PTS["rsi_oversold"]:
        factors["RSI回檔買點"] = FACTOR_PTS["rsi_oversold"]

    # ── OBV volume-price divergence ─────────────────────────
    # ADJUDICATED (backtest_obv.txt, 15y net-of-cost): the BULLISH 量能流入(背離偏多,+10)
    # branch FAILED the weighting gate (CI-lo<=base, no edge over base rate) → DEMOTED to an
    # informational overlay (verdict 'obv_flow' badge / web_export), NOT a score input. The
    # BEARISH 量價背離(出貨警示,-15) PASSED (avoid-filter benefit: flagged names underperform)
    # → KEPT as a live weight. Slope calc retained; the bullish `if`→factor branch is removed.
    o = obv_ind(close, vol)
    obv_s, price_s = slope(o, 20), slope(close, 20)
    if price_s > 0 and obv_s < 0 and FACTOR_PTS["obv_bearish"]:
        factors["量價背離(出貨警示)"] = FACTOR_PTS["obv_bearish"]

    # ── 籌碼集中度 + 外資投信連買 streak (cross-run buffer) ──
    if chips:
        conc = chips.get("conc")
        if conc is not None:
            if conc >= CONC_HIGH and FACTOR_PTS["chip_conc_high"]:
                factors["籌碼集中(法人吸籌)"] = FACTOR_PTS["chip_conc_high"]
            elif CONC_MID <= conc < CONC_HIGH and FACTOR_PTS["chip_conc_mid"]:
                factors["籌碼集中(偏多)"] = FACTOR_PTS["chip_conc_mid"]
            elif conc < 0 and FACTOR_PTS["chip_disperse"]:
                factors["籌碼分散(法人調節)"] = FACTOR_PTS["chip_disperse"]
        st = chips.get("streak", 0) or 0
        if st >= STREAK_MIN and FACTOR_PTS["streak"]:
            factors[f"外資投信連買{st}日"] = FACTOR_PTS["streak"]

    # ── leadership patterns (CI-validated weights, 15y 661-universe run_backtest) ──
    # ONLY signals whose Wilson-CI lower bound cleared the base rate under the FULL
    # multiple-testing family (Bonferroni + BH) on the 661-name universe survive with
    # weight. 2026-06-13 re-gate demoted 首次新高/Power pivot/Trend Template/Pocket pivot/
    # RS線新高 to 0 (82-univ overfit; 首次新高 actually went lift 2.44→0.68, worse than
    # random) and promoted VDU→Thrust (lift 1.61). U/D量比吸籌 kept (lift 1.55). Each
    # factor is additive. Demoted factors are gated by weight>0 so a 0-weight label never
    # enters the factors dict (no verdict pollution). See config LEAD_* + the decision file
    # .decisions/2026-06-13-smartstock-15y-weight-gate.md
    if LEADERSHIP_WEIGHT:
        # B1: data-driven over signal_registry.LEADERSHIP (the single source the backtest also
        # references) instead of 7 hand-repeated if-blocks. Byte-identical: same order, labels,
        # config weight attrs, and predicates. Weight read via config.* so an offline-tuned
        # weight (or an A5 demotion to 0) takes effect without re-import; 0-weight never enters.
        setup = analyze_setup(df)
        for _sig in signal_registry.LEADERSHIP:
            _w = getattr(config, _sig.weight_attr)
            if _w > 0 and _sig.fires(df, bench, setup):
                factors[_sig.label] = _w

    if config.BUCKET_SCORING:
        score, buckets = _bucket_score(factors)
        return {"score": score, "factors": factors, "buckets": buckets, "insufficient": False}
    return {"score": int(sum(factors.values())), "factors": factors,
            "buckets": None, "insufficient": False}


def _bench_for(sym, frames):
    if not frames:
        return None
    if sym.endswith((".TW", ".TWO")):
        return frames.get("twii")
    # US: this app's US universe is NASDAQ/tech-heavy (the entire core watchlist is NASDAQ-listed),
    # so the NASDAQ Composite (^IXIC) is the more-correct RS benchmark than the broad S&P 500 — in
    # a tech-led tape ^GSPC over-credits tech names with rs_strong they didn't earn, and in a tech
    # selloff lets them dodge rs_weak. Fall back to sp500 if the nasdaq frame is absent.
    # NOTE: explicit `is not None` — `frames.get("nasdaq") or ...` raises "truth value of a DataFrame
    # is ambiguous" when the frame is a real DataFrame, silently dropping EVERY US stock.
    nd = frames.get("nasdaq")
    return nd if nd is not None else frames.get("sp500")


def _bare(sym):
    """Bare code: '2330.TW'->'2330', '8069.TWO'->'8069'. Replaces the sym.replace('.TW','') bug
    which corrupted '.TWO' tickers to '8069O' (so the bare-code institutional/chips fallback never
    matched any TPEx name). Strip '.TWO' BEFORE '.TW' (longer suffix first)."""
    for sfx in (".TWO", ".TW"):
        if sym.endswith(sfx):
            return sym[: -len(sfx)]
    return sym


def _inputs_complete(sym, sector, inst, chips):
    """True when every input OBTAINABLE for this symbol's market was present.

    Cross-board comparability (2026-07-17 audit finding 4) caps any row scored on a REDUCED
    input set, so that a 板塊外 row can never claim 🟢 買入 on the watchlist scale. 'Reduced'
    only means anything measured against what the symbol's market can actually supply:
    三大法人 comes from TWSE T86 and the 籌碼 buffer is derived from those same flows, so BOTH
    exist for TW/TPEx listings ONLY. Demanding them from a US row marks a fully-supplied row
    incomplete forever — measured 2026-08-03: it capped every US core pick at 觀察 permanently
    (MSFT/AAPL at 118 would have shown 觀察, not 買入).

    THE one definition of this invariant — callers read the row's `inputs_complete` field and
    NEVER re-derive it, or the two copies drift apart."""
    if sector is None:
        return False
    if sym.endswith((".TW", ".TWO")):
        return inst is not None and chips is not None
    return True


def rank_stocks(data_dict, sector_map=None, institutional_map=None, frames=None, chips_map=None):
    """Score + rank {symbol: DataFrame}. frames = {twii, sp500} for RS;
    chips_map = {sym: {conc, streak}} for 籌碼 factors."""
    sector_map = sector_map if sector_map is not None else SECTOR_MAP
    institutional_map = institutional_map or {}
    chips_map = chips_map or {}
    results = []
    _scoring_errors = 0
    for sym, df in data_dict.items():
        try:
            sector = sector_map.get(sym)
            inst = institutional_map.get(sym) or institutional_map.get(_bare(sym))
            chips = chips_map.get(sym) or chips_map.get(_bare(sym))
            r = score_stock(df, sector=sector, institutional=inst,
                            bench=_bench_for(sym, frames), chips=chips)
            if r.get("insufficient"):
                continue
            results.append({
                "stock": sym,
                "name": STOCK_NAMES.get(sym) or STOCK_NAMES.get(sym + ".TW") or None,
                "score": r["score"],
                "factors": r["factors"],
                "sector": sector,
                # 2026-07-17 audit finding 4: cross-board comparability flag. True only when
                # this row was scored with every input its market can supply (see
                # _inputs_complete). Rows scored on price factors only (opportunity scan /
                # keyless panel / US coverage) are NOT on the core board's scale → verdict
                # assembly caps their light at 觀察 (verdict.capped_entry). Informational
                # output field — never a scoring input.
                "inputs_complete": _inputs_complete(sym, sector, inst, chips),
            })
        except Exception as e:
            # R1-06 audit fix: this used to drop the symbol with zero trace — a config/
            # registry drift or one indicator bug removed the ENTIRE row rather than one
            # factor, and a systemic breakage silently returned an empty board. Now logged
            # + counted so a mass-drop reads as "N symbols failed to score", not "nothing
            # scored well today".
            log.warning("SKIP rank_stocks %s: %s", sym, e)
            _scoring_errors += 1
            continue
    if _scoring_errors:
        log.warning("rank_stocks: %d/%d symbol(s) failed to score", _scoring_errors, len(data_dict))
    return sorted(results, key=lambda x: x["score"], reverse=True)
