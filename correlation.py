# -*- coding: utf-8 -*-
"""Correlation / concentration control (analyst G2).

Surfacing 6 semiconductor names as 6 independent ideas is dangerous — they're ONE
macro bet (rates, TSMC capex, AI demand). A retail user loads all 6 thinking they're
diversified; that's how momentum books blew up in 2022. This computes pairwise return
correlation across the surfaced names, clusters the highly-correlated ones ("treat as
ONE position"), and an 'effective number of bets'. Pure pandas over prices already
fetched; keyless.
"""
import numpy as np
import pandas as pd

CORR_WINDOW = 60
CLUSTER_THRESHOLD = 0.7


def corr_matrix(data, window=CORR_WINDOW):
    """Pairwise return-correlation matrix over the last `window` bars. pandas aligns
    on the date index (handles TW/US calendar gaps). None if <2 usable names."""
    closes = {s: df["Close"] for s, df in (data or {}).items()
              if df is not None and len(df) >= window + 1}
    if len(closes) < 2:
        return None
    px = pd.DataFrame(closes).tail(window + 1)
    rets = px.pct_change().dropna(how="all")
    if len(rets) < 10:
        return None
    return rets.corr()


def clusters(cmat, threshold=CLUSTER_THRESHOLD):
    """Union-find groups of names with pairwise corr ≥ threshold. Returns list of
    lists (each ≥2 names = a correlated cluster to size as one position)."""
    if cmat is None or cmat.empty:
        return []
    syms = list(cmat.columns)
    parent = {s: s for s in syms}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            v = cmat.iloc[i, j]
            if pd.notna(v) and v >= threshold:
                union(syms[i], syms[j])
    groups = {}
    for s in syms:
        groups.setdefault(find(s), []).append(s)
    return [g for g in groups.values() if len(g) >= 2]


def effective_bets(cmat):
    """Effective number of independent bets = N / (1 + (N−1)·avg_pairwise_corr).
    N names that all move together → ~1 effective bet. None if no matrix."""
    if cmat is None or cmat.empty:
        return None
    n = len(cmat.columns)
    if n < 2:
        return float(n)
    iu = np.triu_indices(n, k=1)
    vals = cmat.to_numpy()[iu]
    vals = vals[~np.isnan(vals)]
    if not len(vals):
        return float(n)
    avg = float(np.mean(vals))
    denom = 1 + (n - 1) * max(0.0, avg)
    return round(n / denom, 1) if denom else float(n)


def concentration(data, names=None, window=CORR_WINDOW, threshold=CLUSTER_THRESHOLD):
    """Full read: {clusters:[{names,avg_corr}], effective_bets, n}. clusters carry
    display names. Empty/None-safe."""
    cmat = corr_matrix(data, window)
    if cmat is None:
        return {"clusters": [], "effective_bets": None, "n": len(data or {})}
    names = names or {}
    cl = []
    for g in clusters(cmat, threshold):
        sub = cmat.loc[g, g].to_numpy()
        iu = np.triu_indices(len(g), k=1)
        avg = float(np.nanmean(sub[iu])) if len(g) > 1 else 1.0
        cl.append({"names": [names.get(s) or s for s in g], "tickers": g,
                   "avg_corr": round(avg, 2)})
    cl.sort(key=lambda c: (len(c["tickers"]), c["avg_corr"]), reverse=True)
    return {"clusters": cl, "effective_bets": effective_bets(cmat), "n": len(cmat.columns)}
