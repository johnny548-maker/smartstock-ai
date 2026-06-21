# -*- coding: utf-8 -*-
"""Export each daily report as structured JSON for the PWA, and maintain a
history index. The PWA (GitHub Pages) reads these files — no backend needed."""
import glob
import json
import logging
import math
import os
from datetime import datetime

from config import STOCK_NAMES, DISPLAY_N

log = logging.getLogger(__name__)

# PWA payload schema version (C1). Bump when a breaking shape change ships; the client
# (docs/app.js) soft-banners on a version it doesn't understand. ADDITIVE history rule:
# keep old keys readable / fall back on removal (see the 'breakout' strip) so _rebuild_index
# and the client keep loading mixed-version files. Payloads with no field read as v0.
SCHEMA_VERSION = 1


def _clean(o):
    """Replace NaN/Inf floats with None — they are invalid JSON and break the PWA's
    fetch().json(). Recurse through dicts/lists. Also normalise numpy scalars (np.float32/int64
    are NOT python float/int subclasses, so json.dumps(allow_nan=False) would raise on them and
    abort the WHOLE day's export) via their .item() → native python scalar, then re-clean."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):   # numpy scalar / 0-d array
        try:
            return _clean(o.item())
        except Exception:
            return None
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


def _names_map(opportunity, revenue, scored_universe=None):
    """Display-name map {code: name} for EVERY name the PWA might link to beyond the
    28-stock STOCK_NAMES — opportunity leaders + revenue candidates + the Fix-1
    scored_universe (#1 fix). The PWA's detail view merges this over STOCK_NAMES so a
    bare code never shows where a name exists."""
    names = {}
    for ld in (opportunity or {}).get("leaders", []):
        code, nm = ld.get("ticker"), ld.get("name")
        if code and nm:
            names.setdefault(code, nm)
    for c in (revenue or {}).get("candidates", []):
        code, nm = c.get("code"), c.get("name")
        if code and nm:
            names.setdefault(code, nm)
    for s in (scored_universe or []):
        code, nm = s.get("stock"), s.get("name")
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


def select_scored_universe(opp_ranked, exclude=None, top_n=12):
    """Top-N of the WIDE-universe rank_stocks output, minus the core picks.

    Fix 1 (GAP C): rank_stocks (strategy.py) is universe-agnostic + pure, so the
    daily run scores the ~600-name opportunity universe through the SAME
    backtest-gated formula. This selector trims that scored list to a display
    board: drop any symbol already shown as a core pick, keep rank order, cap N.
    Pure — returns a NEW list, never mutates its input (OVERLAY-NOT-SCORER)."""
    exclude = exclude or set()
    out = [r for r in (opp_ranked or []) if r.get("stock") not in exclude]
    return out[:top_n]


def build_payload(date_str, news, indices, institutional, ranked, analyses,
                  allocation, rebalance_diff, risk, markdown, skips,
                  movers=None, level_map=None, delta=None, events=None, breadth=None,
                  revenue=None, signals=None, themes=None, opportunity=None, pick_cards=None,
                  regime=None, concentration=None, shortvol=None, macro=None, fx=None,
                  watchlist=None, early_board=None, overlays_map=None, source_coverage=None,
                  environment=None, my_positions=None, attribution=None,
                  strategy_health=None, shadow=None, health=None,
                  momentum_portfolio=None, scored_universe=None):
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
        # Perf (#15): the full 60-bar `ohlc` is duplicated byte-identical in the lazy-loaded
        # per-stock detail file (data/detail/<code>.json) which the PWA fetches on click. Drop it
        # from the daily payload (~5KB/pick) — the deck mini-chart uses `spark`, and the detail
        # K-line re-fetches ohlc from the detail file. ~60KB off every visitor's initial download.
        pick.pop("ohlc", None)
        if overlays:
            pick["overlays"] = overlays
        picks.append(pick)
    # search index = every displayed actionable name (picks + opportunity leaders +
    # revenue candidates + top movers) so client-side search/links resolve them all.
    search = _search_index(picks, opportunity, movers, revenue)
    # name map: STOCK_NAMES (the 28 watchlist) + opportunity-leader + revenue-candidate
    # names so the detail view shows names not bare codes for every linkable name.
    names = {**STOCK_NAMES, **_names_map(opportunity, revenue, scored_universe)}
    return {
        "schema_version": SCHEMA_VERSION,   # C1: client compatibility guard
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
        # Strip the internal '_data' key (raw OHLCV DataFrames threaded from
        # universe.get_opportunities for detail-file generation) so json.dump never
        # sees a DataFrame and raises TypeError: Object of type DataFrame is not
        # JSON serializable.
        # R7 DEDUP: also strip 'breakout' — the SAME list is already serialized once
        # as the top-level 'early_board' key (main.py 7d promote). Keeping both
        # doubled every payload by ~15 OHLC-heavy entries. Pure serialization-side
        # strip: the caller's opportunity dict is NEVER mutated (report_builder /
        # sheets_sync still read opp['breakout'] in-memory). app.js falls back to
        # opportunity.breakout only for pre-R7 history files.
        "opportunity": (
            {k: v for k, v in opportunity.items() if k not in ("_data", "breakout")}
            if isinstance(opportunity, dict) else opportunity
        ),
        "regime": regime,
        # P2/P3 market/sector ENVIRONMENT gauges (taifex regime + macro_tw industry + macro_us
        # macro + P3 cftc_cot sector_tilt). Additive top-level section, NOT keyed by ticker,
        # NEVER scored/ranked — surfaced beside the dashboard for context only (golden-additive
        # invariant). The whole dict is carried as-is, so any new gauge key (e.g. P3
        # environment['sector_tilt']) flows through automatically. Backward-compatible:
        # environment defaults to {} so older callers/payloads are unaffected.
        "environment": environment or {},
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
        # P2-S1 我的持倉 block (positions.summarize) — holdings-aware overnight-risk lens.
        # OVERLAY-NOT-SCORER: informational; suggested_stop is DISPLAY-ONLY, never scored.
        # Backward-compatible: defaults to {} so older callers/payloads are unaffected.
        "my_positions": my_positions or {},
        # P2-S1 attribution block (attribution.summarize) — which signals/regimes our picks
        # rode + a hypothetical NAV replay. INFORMATIONAL self-evaluation, NEVER scored.
        # Backward-compatible: defaults to {} (per spec) so older callers are unaffected.
        "attribution": attribution or {},
        # Premortem P-M1/2/3 self-evaluation blocks (strategy_health / shadow /
        # health). INFORMATIONAL overlays for the PWA banner — NEVER summed into
        # scoring/ranking. Backward-compatible: default {} so older callers and
        # payloads are unaffected. main.py may also set these keys post-build
        # (pick_performance idiom) before the re-export.
        "strategy_health": strategy_health or {},
        "shadow": shadow or {},
        "health": health or {},
        "early_board": early_board or [],  # promoted early/breakout 起漲 board (REQ1)
        # 動能組合（季度）lens (momentum_portfolio.build_lens shape) — quarterly top-20
        # 12-1 momentum PORTFOLIO view. SEPARATE FRAMEWORK from the daily picks:
        # momentum is a portfolio-construction factor (proven by backtest_portfolio.py),
        # NEVER summed into strategy.score_stock / rank_stocks (golden-additive invariant).
        # Backward-compatible: defaults to {} so older callers/payloads are unaffected.
        "momentum_portfolio": momentum_portfolio or {},
        # Fix 1 (GAP C) 全市場精選 — top-N of the ~600 opportunity universe scored
        # through the SAME gated rank_stocks formula as the core picks (resource-thin
        # names simply don't fire chip/法人 factors → lower, not penalised). ADDITIVE
        # top-level board; NEVER perturbs picks/score/rank (golden-additive invariant).
        # Backward-compatible: defaults to [] so older callers/payloads are unaffected.
        "scored_universe": scored_universe or [],
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
            top = d["picks"][0] if d.get("picks") else None
            index.append({
                "date": d.get("date"),
                "risk": d.get("risk"),
                "top": top.get("stock") if top else None,      # .get(): a malformed pick must not
                "top_name": top.get("name") if top else None,  #   nuke an otherwise-valid day from
                "top_score": top.get("score") if top else None,  # the history index
                "generated_at": d.get("generated_at"),
            })
        except Exception as e:
            log.warning("SKIP index entry %s: %s", name, e)   # never silently drop a date
            continue
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


def write_universe_index(rows, web_dir):
    """Write data/_universe.json = deduped [[code,name,market],…] for all-market search.

    Fix 3 (GAP A): a SEPARATE lightweight, idempotently-overwritten file (its own
    cache) — never inlined into the per-day payloads. Dedup by code (first wins),
    drop blank codes. Compact separators keep the ~1800-row file small. Returns the
    path. Pure shape; the only side effect is the single file write."""
    seen, out = set(), []
    for r in (rows or []):
        if not r:
            continue
        if isinstance(r, (list, tuple)):
            code = r[0] if len(r) > 0 else None
            name = r[1] if len(r) > 1 else None
            market = r[2] if len(r) > 2 else None
        else:
            code, name, market = r.get("code"), r.get("name"), r.get("market")
        if not code or code in seen:
            continue
        seen.add(code)
        out.append([code, name or code, market or ""])
    data_dir = os.path.join(web_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "_universe.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    return path


def write_verdicts_index(verdict_map, web_dir):
    """Write data/_verdicts.json = {code: {s: score, l: light}} for every scored name.

    #3 (full-market verdict): so the all-market search can show a CURRENT recommendation
    (light → 買入/觀望/不持有) for any name the daily run scored — core picks + the full
    scored opportunity universe + (over time) the keyless panel. A SEPARATE compact cached
    file, idempotently overwritten; never inlined into the per-day payload. Returns the path."""
    data_dir = os.path.join(web_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "_verdicts.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(verdict_map or {}, f, ensure_ascii=False, separators=(",", ":"))
    return path
