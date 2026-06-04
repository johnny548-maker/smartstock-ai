# -*- coding: utf-8 -*-
"""'What changed since yesterday' — diff today's report against the most recent
previous daily JSON. Pure dict diff, no LLM. Used for the ⚡變化 line."""
import glob
import json
import os

from config import WEB_DIR


def load_prev(today_date):
    """Return the most recent docs/data/<date>.json with date < today, or None."""
    data_dir = os.path.join(WEB_DIR, "data")
    dates = []
    for p in glob.glob(os.path.join(data_dir, "*.json")):
        name = os.path.basename(p)
        if name == "index.json" or name.startswith("_"):
            continue
        d = name[:-5]
        if d < today_date:
            dates.append(d)
    if not dates:
        return None
    prev = max(dates)
    try:
        with open(os.path.join(data_dir, f"{prev}.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def compute_delta(today, prev, top_n=5):
    """Return a list of change strings (today/prev are payload-like dicts)."""
    if not prev:
        return ["首份報告，無昨日比較。"]
    changes = []

    t_top = [p["stock"] for p in (today.get("picks") or [])[:top_n]]
    p_top = [p["stock"] for p in (prev.get("picks") or [])[:top_n]]
    new = [s for s in t_top if s not in p_top]
    drop = [s for s in p_top if s not in t_top]
    if new:
        changes.append("新進榜：" + "、".join(new))
    if drop:
        changes.append("掉榜：" + "、".join(drop))

    if today.get("risk") != prev.get("risk"):
        changes.append(f"風險 {prev.get('risk')}→{today.get('risk')}")

    t_inst = today.get("institutional") or {}
    p_inst = prev.get("institutional") or {}
    flips = []
    for code, d in t_inst.items():
        tf = d.get("foreign", 0)
        pf = (p_inst.get(code) or {}).get("foreign", 0)
        if pf > 0 and tf < 0:
            flips.append(f"{code} 外資轉賣超")
        elif pf < 0 and tf > 0:
            flips.append(f"{code} 外資轉買超")
    if flips:
        changes.append("；".join(flips[:4]))

    return changes or ["與昨日無重大變化。"]
