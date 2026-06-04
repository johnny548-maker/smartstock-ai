# -*- coding: utf-8 -*-
"""Theme-emergence detection from news titles (keyless).

The explosive names (台光電/Lumentum/Micron) move *because a theme rotates in*
(AI PCB / 矽光子 / HBM) before the price trend is obvious. This module reads the
same free RSS titles already fetched for the news block, counts theme-keyword
hits, compares against a buffered baseline (EMA), and flags themes whose mention
rate is *accelerating* — then maps each hot theme to its supply-chain tickers so
the rest of the system can cross-reference which candidates ride a live theme.

Pure `detect_themes()` is unit-tested; `get_themes()` wires news + state buffer.
"""
import json
import logging
import os

import config

log = logging.getLogger(__name__)

# ── Theme → (keywords, supply-chain tickers) ────────────────────────────────
# Keywords mix English proper nouns (kept verbatim per user req) + 中文. Tickers
# are the listed names that lead each theme; ADR/foreign names noted but the .TW
# names are what the watchlist/revenue scan can actually act on.
THEMES = {
    "HBM 高頻寬記憶體": {
        "kw": ["HBM", "高頻寬記憶體", "高頻寬記憶", "HBM3", "HBM4"],
        "tickers": ["MU", "2330.TW", "3711.TW"],
    },
    "CoWoS 先進封裝": {
        "kw": ["CoWoS", "先進封裝", "advanced packaging", "2.5D", "3D 封裝", "SoIC"],
        "tickers": ["2330.TW", "3711.TW", "3034.TW", "6533.TW"],
    },
    "CPO 矽光子": {
        "kw": ["CPO", "矽光子", "silicon photonics", "共封裝光學", "光通訊", "光收發",
               "Lumentum", "光模組"],
        "tickers": ["3081.TW", "4977.TW", "3450.TW", "6451.TW"],
    },
    "CCL 銅箔基板/載板": {
        "kw": ["CCL", "銅箔基板", "ABF", "載板", "AI PCB", "高速 PCB", "印刷電路板"],
        "tickers": ["2383.TW", "3037.TW", "8046.TW", "6213.TW"],
    },
    "液冷散熱": {
        "kw": ["液冷", "散熱", "immersion cooling", "liquid cooling", "水冷", "均熱板"],
        "tickers": ["3324.TW", "3017.TW", "6230.TW"],
    },
    "ASIC 客製晶片": {
        "kw": ["ASIC", "客製化晶片", "自研晶片", "客製晶片", "TPU"],
        "tickers": ["2454.TW", "3661.TW", "2329.TW"],
    },
    "AI 伺服器": {
        "kw": ["AI 伺服器", "AI server", "GB200", "GB300", "機櫃", "Blackwell", "輝達 AI"],
        "tickers": ["2317.TW", "2382.TW", "3231.TW", "2376.TW"],
    },
    "機器人": {
        "kw": ["人形機器人", "humanoid", "機器人", "Optimus"],
        "tickers": ["1503.TW", "2049.TW", "1590.TW"],
    },
}

# Emergence thresholds
THEME_MIN_HITS = 2          # need at least this many mentions today to count
THEME_ACCEL_RATIO = 1.5     # today's count ≥ 1.5× baseline EMA → emerging
THEME_EMA_ALPHA = 0.4       # baseline smoothing
THEME_STATE = os.path.join(config.WEB_DIR, "data", "_theme_state.json")


def detect_themes(titles, baseline=None):
    """Pure: count theme keyword hits across `titles`; flag emerging themes.

    A theme is *emerging* when today's hit count ≥ THEME_MIN_HITS and either there
    is no baseline yet OR today's count ≥ THEME_ACCEL_RATIO × baseline. Returns a
    list sorted by (emerging, count) desc:
        [{theme, count, emerging, tickers}]
    """
    baseline = baseline or {}
    text_blobs = [(t or "").lower() for t in (titles or [])]
    out = []
    for name, spec in THEMES.items():
        kws = [k.lower() for k in spec["kw"]]
        count = sum(1 for blob in text_blobs if any(k in blob for k in kws))
        base = baseline.get(name, 0.0)
        emerging = count >= THEME_MIN_HITS and (base <= 0 or count >= THEME_ACCEL_RATIO * base)
        out.append({
            "theme": name,
            "count": count,
            "emerging": bool(emerging),
            "tickers": spec["tickers"],
        })
    out.sort(key=lambda x: (x["emerging"], x["count"]), reverse=True)
    return out


def hot_tickers(themes):
    """Set of tickers belonging to any *emerging* theme — the cross-reference key."""
    s = set()
    for t in themes or []:
        if t.get("emerging"):
            s.update(t.get("tickers", []))
    return s


# ── State buffer (baseline EMA of per-theme counts) ─────────────────────────
def load_state():
    try:
        with open(THEME_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(THEME_STATE), exist_ok=True)
        with open(THEME_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.warning("SKIP theme-state save: %s", e)


def update_baseline(state, themes, alpha=THEME_EMA_ALPHA):
    """EMA-update each theme's baseline with today's count. Returns new state."""
    new = dict(state)
    for t in themes:
        prev = new.get(t["theme"], float(t["count"]))
        new[t["theme"]] = round(alpha * t["count"] + (1 - alpha) * prev, 3)
    return new


def _titles_from_news(news):
    titles = []
    for items in (news or {}).values():
        for it in items:
            if it.get("title"):
                titles.append(it["title"])
    return titles


def get_themes(news):
    """End-to-end: news dict → emerging themes (with baseline buffer updated)."""
    titles = _titles_from_news(news)
    state = load_state()
    themes = detect_themes(titles, baseline=state)
    save_state(update_baseline(state, themes))
    return themes
