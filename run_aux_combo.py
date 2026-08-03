# -*- coding: utf-8 -*-
"""Iteration-2 driver — KEYLESS chip/fundamental factor extension.

Honest pipeline (see .decisions/2026-06-22-chip-fundamental-extension):
  1. load 15y OHLCV cache → close + volume panels (prices)
  2. backfill / load aux raw panels (build_aux_panels) → build PIT-lagged factor panels
  3. Phase-0 IC screen: rank-IC of each aux factor → keep only IC >= IC_MIN (fail-fast)
  4. pre-registered combo gate: inverse-vol blend of (price sleeves + surviving aux sleeves),
     5 gates @ n_trials=2 → honest PASS/FAIL (NEVER loosen the gate to chase a pass, ADR §9)

Run: python run_aux_combo.py [--backfill START END] [--csv universe.csv] [--years N]
Heavy (15y x ~650 names + date-by-date aux backfill) → CI (optimize-sleeve.yml).
"""
import os
import sys
import json
import functools
import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)               # noqa: A001 — survive a kill mid-run

import numpy as np
import pandas as pd

import backtest
import backtest_portfolio as bp
import run_optimize as ro
import build_aux_panels as bap
import build_ohlcv_cache as boc
import run_backtest as rb
from config import IC_MIN

OUT_TXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimize_tw_aux.txt")
OUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimize_tw_aux.json")


def panel_rank_ic(factor_panel, close_df, fwd=20, step=5, min_names=8):
    """Mean cross-sectional rank-IC of a (dates x tickers) factor panel vs forward returns.
    Pure look-ahead-safe IC: at each sampled date, rank names by the factor and Spearman-correlate
    with their realised fwd-day forward return. HIGHER factor should predict HIGHER return → IC>0."""
    if factor_panel is None or getattr(factor_panel, "empty", True) or close_df.empty:
        return None, 0
    # entry at the NEXT bar (mirrors simulate_portfolio's next-open fill) so the screen can't credit
    # a same-close head start the sleeve never gets — closes the value(lag-0) IC look-ahead.
    fwd_ret = close_df.shift(-1 - fwd) / close_df.shift(-1) - 1.0
    idx = close_df.index
    ics = []
    for i in range(0, len(idx) - fwd - 1, step):
        d = idx[i]
        if d not in factor_panel.index:
            continue
        f = factor_panel.loc[d].dropna()
        if f.empty:
            continue
        r = fwd_ret.loc[d].reindex(f.index).dropna()
        common = f.index.intersection(r.index)
        if len(common) >= min_names:
            ics.append(backtest.spearman(list(f.loc[common]), list(r.loc[common])))
    return (float(np.mean(ics)) if ics else None), len(ics)


def screen_survivors(ics, floor=IC_MIN):
    """Keep families whose rank-IC clears the floor (fail-fast pruning before the expensive gate)."""
    return [fam for fam, ic in ics.items() if ic is not None and ic >= floor]


def n_trials_for(ics, prior_combos=1):
    """DSR trial count = prior pre-registered combos (iteration-1 price = 1) + EVERY aux candidate
    IC-evaluated this run. keep-iff-IC≥floor is data-dependent selection, so each screened candidate
    is a trial that must deflate DSR (ADR §6 amended 2026-06-22) — never a flat constant."""
    return prior_combos + len(ics)


def _load_price_panels(years, universe_csv):
    """Build close + volume (dates x tickers) panels from the 15y OHLCV cache (PIT-sliced)."""
    tickers = rb.assemble_main_universe(universe_csv) if universe_csv else []
    tickers = [t for t in tickers if t.endswith(".TW")]    # iteration-2 is TW-only
    prices = {}
    for t in tickers:
        raw = boc.load_df(t, boc.CACHE_DIR)
        if raw is None or getattr(raw, "empty", True):
            continue
        df = rb._slice_years(raw, years)
        clean, fixed = bp.sanitize_ohlcv(df, "TW", max_fix=bp.MAX_FIXED_BARS)
        if len(fixed) > bp.MAX_FIXED_BARS or clean is None or clean.empty:
            continue
        prices[t] = clean
    return prices


def _code_panels(prices):
    """close + volume panels keyed by 4-digit CODE (aux panels are code-keyed, not '.TW')."""
    open_df, close_df = bp.build_panels(prices)
    vol = pd.DataFrame({t: prices[t]["Volume"] for t in prices if t in close_df.columns})
    rename = {t: t.replace(".TW", "").replace(".TWO", "") for t in close_df.columns}
    return close_df.rename(columns=rename), vol.rename(columns=rename)


def gate_aux_combo(prices_tw, configs, aux_tw, n_trials, ledger_path=None):
    """Gate the price+aux combo, threading the lockbox single-evaluation ledger exactly as
    run_optimize.main() does (universe_id is derived inside rigorous_combo from the ticker list).

    The aux run burns a terminal lockbox just like the price-only run does, and until 2026-08-03
    that burn went unrecorded. Its sleeve set differs (price sleeves + IC survivors), so it holds
    its OWN ledger key — gating aux does not spend the price combo's single evaluation."""
    return ro.rigorous_combo(prices_tw, "tw", list(prices_tw), configs=configs,
                             aux=aux_tw, n_trials=n_trials, ledger_path=ledger_path)


def run(years=15, universe_csv=None, backfill_range=None, ledger_path=ro.LOCKBOX_LEDGER):
    prices = _load_price_panels(years, universe_csv)
    if not prices:
        print("ERROR: no TW price frames loaded (build the 15y cache first).")
        sys.exit(1)
    close_code, vol_code = _code_panels(prices)
    print(f"[aux] TW price panel {close_code.shape} (dates x codes)")

    if backfill_range:
        dates = backfill_range
        print(f"[aux] backfilling {len(dates)} trading days from TWSE (keyless)...")
        bap.backfill(dates)
    raw = bap.load_raw_panels()
    print(f"[aux] raw panels loaded: {sorted(raw)}")
    # ACTUAL backfilled span per raw panel (min..max cached date). asof below is TODAY; this line
    # is the antidote to "fresh asof stamp + frozen data" — if the coverage max lags asof, the
    # backfill did not extend and the screen is running on stale inputs.
    bf_coverage = bap.panel_coverage(raw)
    print(f"[aux] backfill coverage (per raw panel [min..max] cached date): {bf_coverage}")

    # margincontra needs 流通股 (shares outstanding) — no keyless multi-year history (quarterly), so
    # it is NOT built (shares_panel=None), excluded honestly like revmom/quality/size. NEVER scored
    # on a volume proxy (different cross-section = unvalidatable factor; ADR §0).
    aux_panels = bap.build_aux_factor_panels(raw, vol_code, shares_panel=None)
    print(f"[aux] factor panels built: {sorted(aux_panels)}  (margincontra excluded — no keyless shares)")

    # Phase-0 IC screen — on the SEARCH SPAN ONLY (exclude the terminal lockbox) so the selection
    # decision is independent of the holdout the gate later scores (no IC-screen leak into lockbox).
    search_idx, _lock_idx = ro.split_lockbox(close_code.index, 0.2)
    close_search = close_code.loc[search_idx]
    ics = {fam: panel_rank_ic(p, close_search)[0] for fam, p in aux_panels.items()}
    survivors = screen_survivors(ics, IC_MIN)
    # n_trials: 1 prior (iteration-1 price combo) + every aux candidate IC-evaluated (keep-iff-IC IS
    # data-dependent selection → it must inflate DSR's trial count). ADR §6 amended 2026-06-22.
    n_trials = n_trials_for(ics)
    print("\n[IC screen, search-span]  factor    rank-IC   gate(>= %.3f)" % IC_MIN)
    for fam, ic in ics.items():
        s = f"{ic:.4f}" if ic is not None else "n/a"
        print(f"            {fam:<13} {s:>8}   {'KEEP' if fam in survivors else 'drop'}")
    print(f"[IC screen] survivors: {survivors or '(none — honest negative; no aux factor clears IC floor)'}"
          f"  | n_trials={n_trials}")

    result = {"asof": datetime.date.today().isoformat(), "backfill_coverage": bf_coverage,
              "ic": ics, "survivors": survivors,
              "ic_min": IC_MIN, "n_trials": n_trials, "excluded": ["margincontra", "revmom", "quality", "size"],
              "gate": None}

    if survivors:
        # combo = price sleeves + surviving aux sleeves; n_trials counts the screened candidates
        configs = dict(ro.PREREG_CONFIGS)
        for fam in survivors:
            configs[fam] = ro.AUX_PREREG_CONFIGS[fam]
        prices_tw = {t: prices[t] for t in prices}
        idx_df = prices_tw.get(bp.SLEEVES["tw"]["index"])
        # aux panels are code-keyed; rigorous_combo's sleeve build is .TW-keyed → reindex aux to .TW
        rename_tw = {c: c + ".TW" for c in close_code.columns}
        aux_tw = {fam: aux_panels[fam].rename(columns=rename_tw) for fam in survivors}
        gate = gate_aux_combo(prices_tw, configs, aux_tw, n_trials, ledger_path=ledger_path)
        result["gate"] = gate
        result["lockbox_ledger"] = gate.get("lockbox_ledger")
        result["lockbox_contaminated"] = bool(
            (gate.get("lockbox_ledger") or {}).get("prior_evals", 0) > 0)
        print("\n[GATE @ n_trials=2]")
        print(json.dumps({k: gate.get(k) for k in
                          ("pass", "dsr", "dsr_pass", "pbo", "pbo_pass", "spa_p", "spa_pass",
                           "lockbox", "lockbox_pass", "flat_lift", "flat_pass", "sleeves")},
                         ensure_ascii=False, indent=2, default=str))
        verdict = "PASS" if gate.get("pass") else "FAIL"
        print(f"\n[VERDICT] {verdict} — " + (
            "real, gate-clearing keyless chip/fundamental edge (→ Phase 2 HITL)" if gate.get("pass")
            else "honest negative result (ADR §9: do NOT loosen the gate or add a family to chase it)"))
    else:
        print("\n[VERDICT] FAIL at IC screen — no aux factor predicts returns; nothing enters the gate.")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write(f"SmartStock iteration-2 aux combo — asof {result['asof']}\n")
        f.write(f"backfill coverage (per raw panel [min..max] cached date): {bf_coverage}\n")
        f.write(f"excluded (no keyless multi-year history): margincontra, revmom, quality, size\n")
        f.write(f"IC screen (search-span, floor {IC_MIN}, next-bar fill): " + json.dumps(ics, default=str) + "\n")
        f.write(f"survivors: {survivors}  | n_trials={result.get('n_trials')}\n")
        if result["gate"]:
            g = result["gate"]
            f.write(f"GATE pass={g.get('pass')} dsr={g.get('dsr')} spa_p={g.get('spa_p')} "
                    f"flat_lift={g.get('flat_lift')} lockbox={g.get('lockbox')}\n")
    print(f"\n[out] {OUT_TXT} / {OUT_JSON}")
    return result


def _parse_args(argv):
    universe_csv = None
    years = 15
    backfill_range = None
    if "--csv" in argv:
        k = argv.index("--csv"); universe_csv = argv[k + 1]
    if "--years" in argv:
        k = argv.index("--years"); years = int(argv[k + 1])
    if "--backfill" in argv:
        k = argv.index("--backfill")
        start, end = argv[k + 1], argv[k + 2]
        backfill_range = [d.strftime("%Y%m%d") for d in pd.bdate_range(start, end)]
    return years, universe_csv, backfill_range


if __name__ == "__main__":
    y, csv, bf = _parse_args(sys.argv)
    try:
        run(years=y, universe_csv=csv, backfill_range=bf)
    except ro.LockboxReuseError as e:
        # Same contract as run_optimize.main(): a re-burn in CI fails the run, it never degrades
        # to a warning — the aux lockbox gets exactly one evaluation too.
        print(f"ERROR {e}", file=sys.stderr)
        sys.exit(2)
