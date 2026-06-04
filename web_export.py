# -*- coding: utf-8 -*-
"""Export each daily report as structured JSON for the PWA, and maintain a
history index. The PWA (GitHub Pages) reads these files — no backend needed."""
import glob
import json
import os
from datetime import datetime


def build_payload(date_str, news, indices, institutional, ranked, analyses,
                  allocation, rebalance_diff, risk, markdown, skips):
    return {
        "date": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "risk": risk,
        "indices": indices,
        "news": news,
        "institutional": institutional,
        "picks": [
            {
                "stock": it["stock"],
                "score": it["score"],
                "sector": it.get("sector"),
                "factors": it["factors"],
                "commentary": (analyses or {}).get(it["stock"]),
            }
            for it in ranked
        ],
        "allocation": allocation,
        "rebalance": rebalance_diff,
        "skips": sorted(set(skips or [])),
        "markdown": markdown,
    }


def _rebuild_index(data_dir):
    index = []
    for path in glob.glob(os.path.join(data_dir, "*.json")):
        if os.path.basename(path) == "index.json":
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
        json.dump(payload, f, ensure_ascii=False, indent=1)
    _rebuild_index(data_dir)
    return data_dir
