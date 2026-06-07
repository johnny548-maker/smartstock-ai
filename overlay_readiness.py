# -*- coding: utf-8 -*-
"""overlay_readiness — monthly overlay-backtest-readiness report.

Reads docs/data/_overlay_history/*.json across all accrued dates; for each overlay
signal-type (grouped by source + kind + normalised label family) counts FIRED EVENTS
(stock-days where it fired) that ALSO have >= HORIZON subsequent snapshot dates
available to measure forward return; for those crossing >= MIN_FIRED with the forward
window, computes a forward hit-rate placeholder and Wilson-CI lower bound using the
same stats helpers as run_backtest.py / backtest.py.

HONEST CONTRACT: With only 1–2 days of history the report will show 'accruing' for
every signal.  The Wilson-CI gate (CI-lower > base) only becomes meaningful once
enough fired events with a full forward window exist.

Usage:
    python overlay_readiness.py [--horizon N] [--min-fired M] [--history-dir PATH]

Output: reports/overlay_readiness_<today>.md  + one-line stdout summary.
No network I/O.  No scoring change.
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta
from collections import defaultdict

# ── Wilson CI (same formula as backtest.wilson_ci) ───────────────────────────
def wilson_ci(k, n, z=1.96):
    """Wilson score interval for k successes in n trials → (lo, hi)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


# ── label-family normaliser ──────────────────────────────────────────────────
_STRIP_DIGITS = re.compile(r"[\d,，+\-+.%（）()]+")
_COLLAPSE_WS = re.compile(r"\s+")

def _label_family(label):
    """Normalise an overlay label to a stable family key.

    Strips numeric values so '三大法人買超 12,345 股' and '三大法人買超 9,999 股'
    collapse to the same family '三大法人買超  股'.
    """
    s = _STRIP_DIGITS.sub(" ", str(label))
    s = _COLLAPSE_WS.sub(" ", s).strip()
    return s


def _signal_key(ov):
    """Stable grouping key: (source, kind, label_family)."""
    return (ov.get("source", ""), ov.get("kind", ""), _label_family(ov.get("label", "")))


# ── history loader ───────────────────────────────────────────────────────────
_HISTORY_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "docs", "data", "_overlay_history",
)


def load_history(history_dir=None):
    """Load all snapshot files → list of (date_str, entry_list).

    Returns sorted list of (date_str, entries) by date ascending.
    Skips files that fail to parse (graceful-skip).
    """
    d = history_dir or _HISTORY_DIR
    result = []
    if not os.path.isdir(d):
        return result
    for fname in os.listdir(d):
        if not fname.endswith(".json"):
            continue
        date_str = fname[:-5]            # strip .json
        # validate YYYY-MM-DD pattern
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            continue
        fpath = os.path.join(d, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                entries = json.load(fh)
            if isinstance(entries, list):
                result.append((date_str, entries))
        except Exception:
            pass   # graceful-skip malformed file
    result.sort(key=lambda x: x[0])
    return result


# ── readiness analysis ───────────────────────────────────────────────────────
def analyse(history, horizon=60, min_fired=100, hit_pct=25.0):
    """Analyse accrued overlay history for backtest readiness.

    Args:
        history:   list of (date_str, entries) sorted ascending.
        horizon:   minimum number of subsequent snapshot dates needed to measure
                   forward return (default 60).
        min_fired: minimum fired-with-horizon events to enter the Wilson-CI gate
                   (default 100, mirrors run_backtest.py FIRED_FLOOR).
        hit_pct:   forward return target percentage (informational; actual hit-rate
                   requires price follow-through data not yet accrued — placeholder 0%).

    Returns: list of row dicts.
    """
    # All snapshot dates (sorted)
    all_dates = [d for d, _ in history]
    n_dates = len(all_dates)
    date_index = {d: i for i, d in enumerate(all_dates)}

    # Collect fired events per signal key
    # fired_events[key] = list of date_str where this signal fired for ANY stock
    fired_events = defaultdict(list)

    for date_str, entries in history:
        for entry in entries:
            for ov in (entry.get("overlays") or []):
                key = _signal_key(ov)
                fired_events[key].append(date_str)

    # Compute readiness per signal key
    rows = []
    for key, fires in sorted(fired_events.items()):
        source, kind, label_fam = key
        total_fired = len(fires)

        # Count fires that have >= horizon subsequent snapshot dates available
        fired_with_horizon = 0
        for d in fires:
            idx = date_index.get(d, -1)
            if idx >= 0 and (n_dates - 1 - idx) >= horizon:
                fired_with_horizon += 1

        # Placeholder hit-rate: actual price follow-through not yet computable
        # (would need: entry close + future close at idx+horizon, not yet accrued).
        # We honestly report 0/fired_with_horizon until price follow-through available.
        hit_k = 0
        hit_rate = 0.0
        ci_lo, ci_hi = wilson_ci(hit_k, fired_with_horizon)

        # Base rate placeholder (global base rate requires price series — not keyless here).
        # We set base = 0.0 and mark READY only once fired_with_horizon >= min_fired AND
        # we have real hit-rate data.  Currently always NOT_READY for honest reporting.
        base_rate = 0.0
        ready = (fired_with_horizon >= min_fired and ci_lo > base_rate
                 and hit_k > 0)   # hit_k>0 guards against degenerate CI when no data

        rows.append({
            "source":             source,
            "kind":               kind,
            "label_family":       label_fam,
            "total_fired":        total_fired,
            "fired_with_horizon": fired_with_horizon,
            "hit_rate":           hit_rate,
            "ci_lo":              ci_lo,
            "base_rate":          base_rate,
            "n_dates_history":    n_dates,
            "horizon":            horizon,
            "ready":              ready,
        })

    return rows


# ── report writer ────────────────────────────────────────────────────────────
_REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "reports",
)


def write_report(rows, today_str=None, n_dates_history=0, out_dir=None):
    """Write reports/overlay_readiness_<today>.md.  Returns the output path."""
    today = today_str or date.today().strftime("%Y-%m-%d")
    out = out_dir or _REPORTS_DIR
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f"overlay_readiness_{today}.md")

    n_signals = len(rows)
    n_ready = sum(1 for r in rows if r["ready"])
    n_accruing = n_signals - n_ready
    min_fired_used = rows[0]["horizon"] if rows else 60   # horizon from first row
    min_fired_floor = 100                                  # mirrors run_backtest.FIRED_FLOOR

    lines = [
        f"# Overlay Backtest Readiness — {today}",
        "",
        f"**History:** {n_dates_history} daily snapshot date(s) accrued in "
        f"`docs/data/_overlay_history/`.",
        f"**Horizon gate:** {min_fired_used} subsequent snapshot dates required to "
        f"measure forward return.",
        f"**Min-fired floor:** {min_fired_floor} fired-with-horizon events (mirrors "
        f"`run_backtest.FIRED_FLOOR`).",
        f"**Wilson-CI gate:** CI-lower > base rate (same gate as `run_backtest.main()`).",
        "",
        f"**Summary:** {n_ready}/{n_signals} signal families READY "
        f"({n_accruing} accruing — not yet backtestable).",
        "",
        "| signal-family | source | kind | fired-total | "
        "fired-w-horizon | hit-rate | wilson-ci-lower | base | READY? | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    for r in rows:
        verdict = "READY" if r["ready"] else (
            f"accruing — {r['n_dates_history']} days history, "
            f"{r['total_fired']} fired, not ready"
        )
        hit_pct_str = f"{r['hit_rate']:.1%}" if r["fired_with_horizon"] > 0 else "n/a"
        ci_str = f"{r['ci_lo']:.3f}" if r["fired_with_horizon"] > 0 else "n/a"
        base_str = f"{r['base_rate']:.3f}"
        lines.append(
            f"| {r['label_family'][:50]} | {r['source']} | {r['kind']} | "
            f"{r['total_fired']} | {r['fired_with_horizon']} | "
            f"{hit_pct_str} | {ci_str} | {base_str} | "
            f"{'YES' if r['ready'] else 'NO'} | {verdict} |"
        )

    lines += [
        "",
        "---",
        "",
        "> **Note:** Hit-rate and Wilson-CI are placeholders (0/n) until "
        "`fired_with_horizon >= 1` AND price follow-through data (close at "
        "`date + horizon`) is computable from the accrued snapshots.  "
        "The snapshots only store `close` at signal-fire date; forward close "
        "will be inferred once sufficient history exists.",
        "",
        f"*Generated: {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')} UTC*",
    ]

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


# ── CLI entrypoint ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Overlay backtest-readiness report")
    parser.add_argument("--horizon",    type=int, default=60,
                        help="Subsequent snapshot dates needed (default 60)")
    parser.add_argument("--min-fired",  type=int, default=100,
                        help="Fired-with-horizon floor (default 100)")
    parser.add_argument("--history-dir", default=None,
                        help="Override _overlay_history dir")
    args = parser.parse_args()

    history = load_history(args.history_dir)
    n_dates = len(history)

    rows = analyse(history, horizon=args.horizon, min_fired=args.min_fired)

    today_str = date.today().strftime("%Y-%m-%d")
    out_path = write_report(rows, today_str=today_str, n_dates_history=n_dates)

    # One-line stdout summary
    n_ready = sum(1 for r in rows if r["ready"])
    total_fired = sum(r["total_fired"] for r in rows)
    print(
        f"overlay_readiness: {n_dates} day(s) history | "
        f"{len(rows)} signal families | "
        f"{total_fired} total fired events | "
        f"{n_ready}/{len(rows)} READY | "
        f"report -> {out_path}"
    )
    return out_path


if __name__ == "__main__":
    main()
