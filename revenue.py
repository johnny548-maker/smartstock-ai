# -*- coding: utf-8 -*-
"""Taiwan 月營收 (monthly revenue) — the LEADING fundamental spine.

ONE keyless TWSE call (t187ap05_L) returns every listed company's latest-month
revenue with 當月 + 去年當月 → market-wide YoY in a single request, no per-stock
fetch. This is how a 台光電-type name surfaces EARLY even if it's not in the
watchlist. YoY history is buffered cross-run (chip_state pattern) so 3-month
acceleration emerges as the cron accumulates months.
"""
import json
import logging
import os

import requests

from config import (TWSE_REVENUE_URL, REVENUE_STATE, REV_MIN_YOY,
                    EARLY_CANDIDATE_N, REV_ACCEL_MONTHS, REV_BUFFER_MONTHS, TWSE_TIMEOUT,
                    REV_YOY_CEILING, REV_MIN_REVENUE, REV_EXCLUDE_INDUSTRIES)

log = logging.getLogger(__name__)
_UA = {"User-Agent": "Mozilla/5.0"}

K_CODE = "公司代號"
K_NAME = "公司名稱"
K_IND = "產業別"
K_YM = "資料年月"
K_CUR = "營業收入-當月營收"
K_LM = "營業收入-上月營收"
K_LY = "營業收入-去年當月營收"


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def parse_rows(rows):
    """Pure: TWSE rows → [{code,name,industry,ym,cur,yoy,mom}] (YoY/MoM from raw)."""
    out = []
    for r in rows:
        code = str(r.get(K_CODE, "")).strip()
        if not (code.isdigit() and len(code) == 4):
            continue
        cur, ly, lm = _num(r.get(K_CUR)), _num(r.get(K_LY)), _num(r.get(K_LM))
        yoy = round((cur / ly - 1) * 100, 1) if (cur is not None and ly and ly > 0) else None
        mom = round((cur / lm - 1) * 100, 1) if (cur is not None and lm and lm > 0) else None
        out.append({
            "code": code, "name": str(r.get(K_NAME, "")).strip(),
            "industry": str(r.get(K_IND, "")).strip(), "ym": str(r.get(K_YM, "")).strip(),
            "cur": cur, "yoy": yoy, "mom": mom,
        })
    return out


def load_state():
    try:
        with open(REVENUE_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"stocks": {}}


def update_state(state, recs):
    st = state.setdefault("stocks", {})
    for rec in recs:
        if rec["yoy"] is None or not rec["ym"]:
            continue
        e = st.setdefault(rec["code"], {"name": rec["name"], "yoy": {}})
        e["name"] = rec["name"]
        e["yoy"][rec["ym"]] = rec["yoy"]
        if len(e["yoy"]) > REV_BUFFER_MONTHS:
            for k in sorted(e["yoy"])[:-REV_BUFFER_MONTHS]:
                e["yoy"].pop(k, None)
    return state


def save_state(state):
    os.makedirs(os.path.dirname(REVENUE_STATE), exist_ok=True)
    with open(REVENUE_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def accelerating(state, code, months=REV_ACCEL_MONTHS):
    """True if YoY strictly rises over the last `months` buffered months."""
    y = state.get("stocks", {}).get(code, {}).get("yoy", {})
    if len(y) < months:
        return False
    vals = [y[k] for k in sorted(y)[-months:]]
    return all(vals[i] > vals[i - 1] for i in range(1, len(vals)))


def _is_candidate(r, min_yoy, ceiling):
    """Reject base-effect spikes, lumpy industries, micro-caps."""
    yoy = r["yoy"]
    if yoy is None or not (min_yoy <= yoy <= ceiling):
        return False
    if r.get("cur") is not None and r["cur"] < REV_MIN_REVENUE:
        return False
    ind = r.get("industry", "")
    if any(x in ind for x in REV_EXCLUDE_INDUSTRIES):
        return False
    return True


def rank_candidates(recs, state=None, top=EARLY_CANDIDATE_N, min_yoy=REV_MIN_YOY,
                    ceiling=REV_YOY_CEILING):
    """Filter (base-effect/lumpy/micro), flag 3-month acceleration, sort (accel, yoy)."""
    cands = [dict(r) for r in recs if _is_candidate(r, min_yoy, ceiling)]
    for r in cands:
        r["accel"] = accelerating(state, r["code"]) if state else False
    cands.sort(key=lambda r: (r["accel"], r["yoy"]), reverse=True)
    return cands[:top]


def fetch():
    r = requests.get(TWSE_REVENUE_URL, timeout=TWSE_TIMEOUT, headers=_UA)
    r.raise_for_status()
    return parse_rows(r.json())


def get_early_candidates():
    """Fetch market-wide revenue, update buffer, return {ym, candidates}."""
    recs = fetch()
    state = load_state()
    update_state(state, recs)
    save_state(state)
    ym = recs[0]["ym"] if recs else None
    return {"ym": ym, "candidates": rank_candidates(recs, state)}
