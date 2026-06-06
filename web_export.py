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


def _search_index(picks, opportunity, movers, revenue=None):
    """Flat searchable index of the day's actionable names (code + name + light +
    where to find it). Client-side search filters this — no backend needed.

    Covers EVERY displayed name so search/links resolve: picks, opportunity leaders
    (key `.ticker`), revenue candidates (key `.code`), and the top movers."""
    idx, seen = [], set()

    def add(code, name, light, kind, price=None):
        if not code or code in seen:
            return
        seen.add(code)
        idx.append({"code": code, "name": name or code, "light": light,
                    "kind": kind, "price": price})

    for p in picks:
        add(p["stock"], p.get("name"), p.get("light"), "pick", p.get("price"))
    for ld in (opportunity or {}).get("leaders", []):
        add(ld["ticker"], ld.get("name"), ld.get("light"), "opportunity", ld.get("price"))
    for c in (revenue or {}).get("candidates", []):
        add(c.get("code"), c.get("name"), None, "revenue")
    for m in (movers or [])[:8]:
        add(m["stock"], None, None, "mover")
    return idx


def _names_map(opportunity, revenue):
    """Display-name map {code: name} for EVERY name the PWA might link to beyond the
    28-stock STOCK_NAMES — opportunity leaders + revenue candidates. The PWA's detail
    view merges this over STOCK_NAMES so a bare code never shows where a name exists."""
    names = {}
    for ld in (opportunity or {}).get("leaders", []):
        code, nm = ld.get("ticker"), ld.get("name")
        if code and nm:
            names.setdefault(code, nm)
    for c in (revenue or {}).get("candidates", []):
        code, nm = c.get("code"), c.get("name")
        if code and nm:
            names.setdefault(code, nm)
    return names


def _overlays_for(symbol, overlays_map):
    """Resolve a symbol's overlay list from {code -> [overlay]}, trying the full
    symbol AND its bare TWSE code ('2330.TW' → also '2330'). Returns [] if none.
    Pure read — never mutates overlays_map (OVERLAY-NOT-SCORER; informational only)."""
    if not overlays_map:
        return []
    out = list(overlays_map.get(symbol, []))
    bare = symbol.replace(".TWO", "").replace(".TW", "")
    if bare != symbol:
        out = out + list(overlays_map.get(bare, []))
    return out


def build_payload(date_str, news, indices, institutional, ranked, analyses,
                  allocation, rebalance_diff, risk, markdown, skips,
                  movers=None, level_map=None, delta=None, events=None, breadth=None,
                  revenue=None, signals=None, themes=None, opportunity=None, pick_cards=None,
                  regime=None, concentration=None, shortvol=None, macro=None, fx=None,
                  watchlist=None, early_board=None, overlays_map=None, source_coverage=None):
    level_map = level_map or {}
    pick_cards = pick_cards or {}
    overlays_map = overlays_map or {}
    picks = []
    for it in ranked[:DISPLAY_N]:
        card = pick_cards.get(it["stock"]) or {}
        # carry the attached overlays through; if attach was SKIPped on the card, fall
        # back to overlays_map directly so the informational overlays still reach the PWA.
        # OVERLAY-NOT-SCORER: 'overlays' is a sidecar key; score/factors are untouched.
        overlays = card.get("overlays") or _overlays_for(it["stock"], overlays_map)
        pick = {
            "stock": it["stock"],
            "name": it.get("name"),
            "score": it["score"],
            "sector": it.get("sector"),
            "factors": it["factors"],
            "levels": level_map.get(it["stock"]),
            "commentary": (analyses or {}).get(it["stock"]),
            **card,                                   # light/verdict/vol_ratio/sr/spark
        }
        if overlays:
            pick["overlays"] = overlays
        picks.append(pick)
    # search index = every displayed actionable name (picks + opportunity leaders +
    # revenue candidates + top movers) so client-side search/links resolve them all.
    search = _search_index(picks, opportunity, movers, revenue)
    # name map: STOCK_NAMES (the 28 watchlist) + opportunity-leader + revenue-candidate
    # names so the detail view shows names not bare codes for every linkable name.
    names = {**STOCK_NAMES, **_names_map(opportunity, revenue)}
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
        "opportunity": opportunity,
        "regime": regime,
        "fx": fx,           # B9 USD/TWD spot context overlay (DISPLAY-ONLY; never scored)
        "macro": macro,     # FRED macro RISK-CONTEXT overlay (informational; never scored)
        "concentration": concentration,
        "shortvol": shortvol or [],     # FINRA RegSHO short-volume board (informational, US-only)
        "indices": indices,
        "news": news,
        "institutional": institutional,
        "movers": movers or [],
        "names": names,
        "watchlist": watchlist or [],     # REQ3b continuous watchlist board (informational)
        "early_board": early_board or [],  # promoted early/breakout 起漲 board (REQ1)
        "picks": picks,
        "search": search,
        "allocation": allocation,
        "rebalance": rebalance_diff,
        # which overlay sources returned data today + their counts (informational).
        "source_coverage": source_coverage or {},
        "skips": sorted(set(skips or [])),
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
