# -*- coding: utf-8 -*-
"""Export each daily report as structured JSON for the PWA, and maintain a
history index. The PWA (GitHub Pages) reads these files — no backend needed."""
import glob
import json
import math
import os
from datetime import datetime

from config import STOCK_NAMES, DISPLAY_N


def _clean(o):
    """Replace NaN/Inf floats with None — they are invalid JSON and break the PWA's
    fetch().json(). Recurse through dicts/lists."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def _tldr(risk, institutional, ranked, breadth=None):
    net = sum((d.get("foreign") or 0) for d in (institutional or {}).values())
    parts = [f"風險 {risk}"]
    if breadth:
        parts.append(f"參與度 {breadth['label']}")
    if institutional:
        parts.append(f"外資合計 {net:+,} 股")
    if ranked:
        top = ranked[0]
        nm = top.get("name") or top["stock"]
        parts.append(f"首選 {nm}（{top['stock']}）分數 {top['score']}")
    return "；".join(parts)


def build_payload(date_str, news, indices, institutional, ranked, analyses,
                  allocation, rebalance_diff, risk, markdown, skips,
                  movers=None, level_map=None, delta=None, events=None, breadth=None,
                  revenue=None, signals=None, themes=None):
    level_map = level_map or {}
    return {
        "date": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "risk": risk,
        "tldr": _tldr(risk, institutional, ranked, breadth),
        "delta": delta or [],
        "events": events or [],
        "breadth": breadth,
        "revenue": revenue,
        "signals": (signals or {}).get("board", []),
        "themes": [t for t in (themes or []) if t.get("emerging")],
        "indices": indices,
        "news": news,
        "institutional": institutional,
        "movers": movers or [],
        "names": STOCK_NAMES,
        "picks": [
            {
                "stock": it["stock"],
                "name": it.get("name"),
                "score": it["score"],
                "sector": it.get("sector"),
                "factors": it["factors"],
                "levels": level_map.get(it["stock"]),
                "commentary": (analyses or {}).get(it["stock"]),
            }
            for it in ranked[:DISPLAY_N]
        ],
        "allocation": allocation,
        "rebalance": rebalance_diff,
        "skips": sorted(set(skips or [])),
        "markdown": markdown,
    }


def _rebuild_index(data_dir):
    index = []
    for path in glob.glob(os.path.join(data_dir, "*.json")):
        name = os.path.basename(path)
        if name == "index.json" or name.startswith("_"):  # skip index + state files
            continue
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        top = d["picks"][0] if d.get("picks") else None
        index.append({
            "date": d.get("date"),
            "risk": d.get("risk"),
            "top": top["stock"] if top else None,
            "top_name": top.get("name") if top else None,
            "top_score": top["score"] if top else None,
            "generated_at": d.get("generated_at"),
        })
    index.sort(key=lambda x: x.get("date") or "", reverse=True)
    with open(os.path.join(data_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    return index


def export(payload, web_dir):
    """Write data/<date>.json and rebuild data/index.json. Returns data dir."""
    data_dir = os.path.join(web_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, f"{payload['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(_clean(payload), f, ensure_ascii=False, indent=1, allow_nan=False)
    _rebuild_index(data_dir)
    return data_dir
