# -*- coding: utf-8 -*-
"""Pre-trade checklist overlay (M3) — keyless, deterministic, INFORMATIONAL.

OVERLAY-NOT-SCORER: build_checklist synthesises five existing gates into a
glanceable yes/no card shown BESIDE a pick. Its output never enters
strategy.score_stock, rank_stocks, or any scoring weight. All five checks
read shapes already produced by other modules (market_regime, earnings_guard,
risk_sizing / verdict) — zero new signals, zero new scoring.

Five gates
~~~~~~~~~~
① regime     exposure_dial >= 50%  (market_regime output shape)
② blackout   no earnings within 7d (earnings_guard.blackout_from_date shape)
③ cluster    same-cluster pick count < 3
④ liquidity  liq_thin=False AND size_ceiling > 0  (verdict.liquidity + risk_sizing shapes)
⑤ rr         risk_plan rr >= 2.0  (risk_sizing.plan shape)

Missing / malformed inputs → that gate's pass=null, detail="資料不足".
Errors in one gate never crash others (graceful-skip).
"""
import logging

log = logging.getLogger(__name__)

# ── gate thresholds (mirrors existing module constants) ──────────────────────
_REGIME_MIN_EXPOSURE = 50       # mirrors market_regime BASE_EXPOSURE thresholds
_CLUSTER_MAX = 3                # same-cluster picks < 3 → pass
_MIN_RR = 2.0                   # mirrors risk_sizing.MIN_RR


# ── internal gate evaluators ─────────────────────────────────────────────────

def _gate_regime(regime):
    """① Exposure dial ≥ 50%.

    regime: market_regime() output shape → {exposure: int, label: str, detail: ...}
    Returns (pass_bool_or_null, detail_str).
    """
    if regime is None:
        return None, "資料不足（無市場體制資料）"
    try:
        exposure = regime.get("exposure")
    except AttributeError:
        return None, "資料不足（無效 regime 結構）"
    if exposure is None:
        return None, "資料不足（exposure 欄位缺失）"
    try:
        exposure = int(exposure)
    except (TypeError, ValueError):
        return None, "資料不足（exposure 非數值）"
    passed = exposure >= _REGIME_MIN_EXPOSURE
    label = regime.get("label", "")
    detail = f"曝險度 {exposure}%（{label}）— 門檻 ≥ {_REGIME_MIN_EXPOSURE}%"
    return passed, detail


def _gate_blackout(earnings_flag):
    """② 財報黑窗：7 日內無財報？

    earnings_flag: earnings_guard.blackout_from_date() output → None or
    {in_blackout: bool, days_until: int, date: str}
    None → no upcoming earnings known → pass.
    """
    if earnings_flag is None:
        return True, "無近期財報（7 日內）"
    try:
        in_blackout = earnings_flag.get("in_blackout")
    except AttributeError:
        return None, "資料不足（無效 earnings_flag 結構）"
    if in_blackout is None:
        # dict present but key absent — treat as unknown
        return None, "資料不足（in_blackout 欄位缺失）"
    if in_blackout:
        days = earnings_flag.get("days_until", "?")
        date_str = earnings_flag.get("date", "")
        detail = f"財報黑窗：{days} 日後（{date_str}）— 跳過新倉"
        return False, detail
    return True, "近期無財報（7 日外）"


def _gate_cluster(concentration):
    """③ 集群：與現有 top picks 同 cluster 數 < 3？

    concentration: int count of same-cluster picks already selected,
    or None if cluster data unavailable.
    """
    if concentration is None:
        return None, "資料不足（無集群資料）"
    try:
        count = int(concentration)
    except (TypeError, ValueError):
        return None, "資料不足（集群計數非整數）"
    passed = count < _CLUSTER_MAX
    detail = f"同集群已有 {count} 檔（門檻 < {_CLUSTER_MAX}）"
    return passed, detail


def _gate_liquidity(risk_plan):
    """④ 流動性/部位上限：liq_thin=False 且 size_ceiling > 0？

    risk_plan: merged dict from verdict.liquidity + risk_sizing.plan shapes.
    Expected keys: liq_thin (bool), size_ceiling (numeric).
    """
    if not risk_plan:
        return None, "資料不足（無風險計畫）"
    try:
        liq_thin = risk_plan.get("liq_thin")
        size_ceiling = risk_plan.get("size_ceiling")
    except AttributeError:
        return None, "資料不足（無效 risk_plan 結構）"

    # Both keys must be present to make a determination
    if liq_thin is None and size_ceiling is None:
        return None, "資料不足（流動性欄位缺失）"

    reasons = []
    passed = True

    if liq_thin:
        passed = False
        reasons.append("流動性不足（liq_thin）")

    if size_ceiling is not None:
        try:
            ceiling = float(size_ceiling)
            if ceiling <= 0:
                passed = False
                reasons.append(f"部位上限 {ceiling} ≤ 0")
            else:
                reasons.append(f"部位上限 {ceiling:,.0f}")
        except (TypeError, ValueError):
            passed = False
            reasons.append("部位上限非數值")
    else:
        # liq_thin present but size_ceiling missing — partial data
        if not liq_thin:
            # thin=False but no ceiling info → still informative, assume pass on thin
            reasons.append("部位上限資料缺")

    detail = "；".join(reasons) if reasons else ("通過" if passed else "不通過")
    return passed, detail


def _gate_rr(risk_plan):
    """⑤ R:R ≥ 2.0？

    risk_plan: risk_sizing.plan() output shape → {rr: float, rr_ok: bool, ...}
    """
    if not risk_plan:
        return None, "資料不足（無風險計畫）"
    try:
        rr = risk_plan.get("rr")
    except AttributeError:
        return None, "資料不足（無效 risk_plan 結構）"
    if rr is None:
        return None, "資料不足（無 R:R 資料）"
    try:
        rr_val = float(rr)
    except (TypeError, ValueError):
        return None, "資料不足（R:R 非數值）"
    passed = rr_val >= _MIN_RR
    detail = f"R:R {rr_val:.1f}（門檻 ≥ {_MIN_RR:.1f}）"
    return passed, detail


# ── public API ────────────────────────────────────────────────────────────────

def build_checklist(pick, regime, concentration, risk_plan, earnings_flag):
    """Build a pre-trade checklist for a single pick candidate.

    All five gates are evaluated from already-computed module outputs — this
    function is a pure synthesiser with no new signals and no scoring.

    Parameters
    ----------
    pick : str | None
        Ticker symbol (informational; not used in gate logic).
    regime : dict | None
        market_regime() output: {exposure, label, detail}.
    concentration : int | None
        Count of existing top picks in the same cluster.
    risk_plan : dict | None
        Merged dict carrying keys from verdict.liquidity (liq_thin, size_ceiling)
        and risk_sizing.plan (rr, rr_ok).
    earnings_flag : dict | None
        earnings_guard.blackout_from_date() output: {in_blackout, days_until, date}
        or None when no upcoming earnings are known.

    Returns
    -------
    dict with:
        items       : list of 5 {key, label, pass, detail} dicts
                      pass is True/False/None (null = data unavailable)
        verdict_line: one Chinese sentence summarising the gate results
    """
    # Evaluate all five gates; each returns (pass_or_null, detail_str).
    # Errors in one gate must not crash others.
    gates = []

    try:
        p, d = _gate_regime(regime)
    except Exception as exc:
        log.warning("pretrade regime gate error: %s", exc)
        p, d = None, "資料不足（計算錯誤）"
    gates.append({"key": "regime", "label": "市場體制（曝險度 ≥ 50%）", "pass": p, "detail": d})

    try:
        p, d = _gate_blackout(earnings_flag)
    except Exception as exc:
        log.warning("pretrade blackout gate error: %s", exc)
        p, d = None, "資料不足（計算錯誤）"
    gates.append({"key": "blackout", "label": "財報黑窗（7 日內無財報）", "pass": p, "detail": d})

    try:
        p, d = _gate_cluster(concentration)
    except Exception as exc:
        log.warning("pretrade cluster gate error: %s", exc)
        p, d = None, "資料不足（計算錯誤）"
    gates.append({"key": "cluster", "label": "集群集中度（同群 < 3 檔）", "pass": p, "detail": d})

    try:
        p, d = _gate_liquidity(risk_plan)
    except Exception as exc:
        log.warning("pretrade liquidity gate error: %s", exc)
        p, d = None, "資料不足（計算錯誤）"
    gates.append({"key": "liquidity", "label": "流動性/部位上限", "pass": p, "detail": d})

    try:
        p, d = _gate_rr(risk_plan)
    except Exception as exc:
        log.warning("pretrade rr gate error: %s", exc)
        p, d = None, "資料不足（計算錯誤）"
    gates.append({"key": "rr", "label": "R:R ≥ 2.0", "pass": p, "detail": d})

    # ── verdict_line ──────────────────────────────────────────────────────────
    verdict = _build_verdict(gates)

    return {"items": gates, "verdict_line": verdict}


def _build_verdict(gates):
    """One-line Chinese verdict from the gate results.

    Examples:
        "5/5 過——條件齊"
        "3/5——黑窗+集群擁擠，等"
        "0/5 資料不足"
    """
    total = len(gates)
    passed = sum(1 for g in gates if g["pass"] is True)
    failed = [g for g in gates if g["pass"] is False]
    null_count = sum(1 for g in gates if g["pass"] is None)

    if null_count == total:
        return f"0/{total} 資料不足——無法判斷"

    # Build fail reason tokens for the sentence
    _fail_tokens = {
        "regime": "市場體制不佳",
        "blackout": "黑窗期",
        "cluster": "集群擁擠",
        "liquidity": "流動性不足",
        "rr": "R:R 不足",
    }

    if passed == total:
        return f"{passed}/{total} 過——條件齊"

    fail_labels = [_fail_tokens.get(g["key"], g["key"]) for g in failed]
    fail_str = "+".join(fail_labels) if fail_labels else ""
    suffix = f"——{fail_str}，等" if fail_str else "——部分條件未達"

    return f"{passed}/{total}{suffix}"
