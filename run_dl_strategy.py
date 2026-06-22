# -*- coding: utf-8 -*-
"""Deep-learning stock-selection experiment (the user's 'use DL to beat the index' request), run
HONESTLY through the same strict OOS gate as everything else. A PyTorch MLP learns to predict each
stock's cross-sectionally-DEMEANED forward 20-day return (demeaning removes market beta → the model
must find SELECTION skill, not 'ride the bull') from a panel of technical features, trained ONLY on
the search span (with a validation slice for early stopping) and tested ONCE on the lockbox holdout.
Predictions feed the SAME tested top-20 monthly portfolio engine; the result is compared to 0050.

DATA HONESTY: keyless full-market TW = TECHNICAL features only (籌碼 concentration/broker-branch =
paid; financials cover ~145 names, not 744). And the universe is still SURVIVOR-biased. So this is
the most generous keyless DL can be — if it fails OOS here, it fails.

LEAKAGE GUARDS (the #1 way DL backtests lie): cross-sectional z-score per day (no future stats);
train feature dates restricted so their +20d target stays inside the search span; test = lockbox
feature dates only; asserted max(train target date) < min(test feature date).

Run: python run_dl_strategy.py
"""
import functools
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)  # noqa: A001

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

import backtest_portfolio as bp
import build_ohlcv_cache as boc
import run_optimize as ro
import screen_price_factors as spf
import validation as val

FWD = 20            # forward-return horizon (trading days)
TOP_N = 20          # long top-20 by prediction, monthly rebalance
SEED = 42


def features_for(df):
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    f = {}
    for n in (5, 10, 20, 60, 120):
        f["ret%d" % n] = c / c.shift(n) - 1.0
    f["vol20"] = c.pct_change().rolling(20).std()
    f["vol60"] = c.pct_change().rolling(60).std()
    d = c.diff()
    up = d.clip(lower=0).rolling(14).mean()
    dn = (-d.clip(upper=0)).rolling(14).mean()
    f["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    e = lambda x, n: x.ewm(span=n, adjust=False).mean()
    macd = e(c, 12) - e(c, 26)
    f["macdh"] = (macd - e(macd, 9)) / c
    ma20, sd = c.rolling(20).mean(), c.rolling(20).std()
    f["bb"] = (c - (ma20 - 2 * sd)) / ((ma20 + 2 * sd) - (ma20 - 2 * sd))
    f["dma50"] = c / c.rolling(50).mean() - 1.0
    f["dma200"] = c / c.rolling(200).mean() - 1.0
    f["d52h"] = c / c.rolling(252, min_periods=60).max() - 1.0
    f["volr"] = v / v.rolling(20).mean()
    return pd.DataFrame(f, index=df.index)


class MLP(nn.Module):
    def __init__(self, n_in):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_in, 64), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(64, 32), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(32, 16), nn.ReLU(),
            nn.Linear(16, 1))

    def forward(self, x):
        return self.net(x).squeeze(-1)


def main():
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    prices = spf.load_cached_prices("TW", 15)
    _o, close = bp.build_panels(prices)
    ren = {c: c.replace(".TW", "").replace(".TWO", "") for c in close.columns}
    cc = close.rename(columns=ren)
    si, li = ro.split_lockbox(cc.index, 0.2)

    # ---- build long feature/target frame ----
    feat_cols = None
    parts = []
    fwd_panel = (cc.shift(-FWD) / cc - 1.0)
    fwd_demean = fwd_panel.sub(fwd_panel.mean(axis=1), axis=0)        # cross-sectional demean = beta-removed
    for t, df in prices.items():
        code = ren.get(t, t)
        if code not in cc.columns:
            continue
        ff = features_for(df).reindex(cc.index)
        ff["y"] = fwd_demean[code].reindex(cc.index)
        ff["stock"] = code
        ff["date"] = cc.index
        parts.append(ff)
        if feat_cols is None:
            feat_cols = [c for c in ff.columns if c not in ("y", "stock", "date")]
    long = (pd.concat(parts, ignore_index=True)
            .replace([np.inf, -np.inf], np.nan).dropna(subset=feat_cols + ["y"]))
    # cross-sectional z-score per day (no future info), clip outliers — features AND target so MSE
    # isn't blown up by a micro-cap +1000% bar (the cause of the earlier NaN/divergence)
    g = long.groupby("date")
    long[feat_cols] = g[feat_cols].transform(lambda x: (x - x.mean()) / (x.std() + 1e-9)).clip(-5, 5)
    long["y"] = long.groupby("date")["y"].transform(lambda x: (x - x.mean()) / (x.std() + 1e-9)).clip(-5, 5)
    long = long.dropna(subset=feat_cols + ["y"])
    print(f"universe={len(prices)} samples={len(long):,} features={len(feat_cols)} "
          f"device={'cuda' if torch.cuda.is_available() else 'cpu'}")

    # ---- leakage-safe split: train target must stay in search; test = lockbox ----
    search_end = si[-1]
    train_cutoff = si[-(FWD + 1)]                                     # feature date s.t. +FWD still in search
    lock_start = li[0]
    tr = long[long["date"] <= train_cutoff]
    te = long[long["date"] >= lock_start]
    assert tr["date"].max() + pd.Timedelta(days=0) <= search_end
    assert tr["date"].max() < te["date"].min(), "LEAKAGE: train/test overlap"
    # validation = last 15% of train dates (early stopping)
    tdates = np.sort(tr["date"].unique())
    val_start = tdates[int(len(tdates) * 0.85)]
    trn = tr[tr["date"] < val_start]
    vld = tr[tr["date"] >= val_start]
    print(f"  train={len(trn):,} ({pd.Timestamp(tdates[0]).date()}..{pd.Timestamp(val_start).date()}) "
          f"val={len(vld):,} test(lockbox)={len(te):,} ({te['date'].min().date()}..{te['date'].max().date()})")

    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    Xtr = torch.tensor(trn[feat_cols].values, dtype=torch.float32, device=dev)
    ytr = torch.tensor(trn["y"].values, dtype=torch.float32, device=dev)
    Xvl = torch.tensor(vld[feat_cols].values, dtype=torch.float32, device=dev)
    yvl = torch.tensor(vld["y"].values, dtype=torch.float32, device=dev)
    model = MLP(len(feat_cols)).to(dev)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)
    lossf = nn.MSELoss()
    bs = 8192
    best_val, best_state, patience, bad = 1e9, None, 6, 0
    n = len(Xtr)
    for epoch in range(60):
        model.train()
        perm = torch.randperm(n, device=dev)
        for i in range(0, n, bs):
            idx = perm[i:i + bs]
            opt.zero_grad()
            loss = lossf(model(Xtr[idx]), ytr[idx])
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
        model.eval()
        with torch.no_grad():
            vl = lossf(model(Xvl), yvl).item()
        if vl < best_val - 1e-7:
            best_val, best_state, bad = vl, {k: v.clone() for k, v in model.state_dict().items()}, 0
        else:
            bad += 1
            if bad >= patience:
                break
    if best_state:
        model.load_state_dict(best_state)
    print(f"  trained: best val MSE {best_val:.6f}, epochs {epoch+1}")

    # ---- predict lockbox → prediction panel → IC + portfolio vs 0050 ----
    model.eval()
    with torch.no_grad():
        te = te.copy()
        te["pred"] = model(torch.tensor(te[feat_cols].values, dtype=torch.float32, device=dev)).cpu().numpy()
    # lockbox prediction IC (rank corr of pred vs realized demeaned fwd return), per day, averaged
    ics = te.groupby("date").apply(lambda d: d["pred"].corr(d["y"], method="spearman") if len(d) > 5 else np.nan)
    pred_ic = float(ics.mean())
    print(f"\n[LOCKBOX prediction IC] mean daily rank-IC(pred, realized) = {pred_ic:+.4f}  (need >~0.03 to matter)")

    pred_panel = te.pivot_table(index="date", columns="stock", values="pred").reindex(cc.index)
    pred_tw = pred_panel.rename(columns={c: c + ".TW" for c in pred_panel.columns})
    cfg = {"family": "dl", "vol_target": False, "sigma_target": None, "top_n": TOP_N,
           "rebalance": "monthly", "lookback": 20, "trend_ma": None}
    rets = ro.sleeve_daily_rets(cfg, prices, "tw", list(prices), aux={"dl": pred_tw})
    lk = rets.reindex(li).dropna()
    nav = (1 + lk).cumprod()
    cagr = float(nav.iloc[-1] ** (252 / len(lk)) - 1)
    sh = float(lk.mean() / lk.std() * np.sqrt(252)) if lk.std() else 0.0
    dd = float((nav / nav.cummax() - 1).min())
    dsr = val.deflated_sharpe_ratio(float(lk.mean() / lk.std()), n_trials=10, n_obs=len(lk),
                                    skew=float(lk.skew()), kurt=float(lk.kurtosis() + 3))
    b = boc.load_df("0050.TW")["Close"].pct_change().reindex(li).dropna()
    bnav = (1 + b).cumprod()
    bcagr = float(bnav.iloc[-1] ** (252 / len(b)) - 1)
    bsh = float(b.mean() / b.std() * np.sqrt(252))

    print(f"\n[LOCKBOX (OOS) portfolio — DL top-{TOP_N} monthly, net-of-cost]")
    print(f"  DL strategy   CAGR={cagr:+.1%} Sharpe={sh:.2f} MaxDD={dd:.1%} DSR@10={dsr:.3f} {'PASS' if dsr>0.95 else 'FAIL'}")
    print(f"  buy-hold 0050 CAGR={bcagr:+.1%} Sharpe={bsh:.2f}")
    print(f"  edge          CAGR {cagr-bcagr:+.1%} / Sharpe {sh-bsh:+.2f}")
    beats = (pred_ic > 0.03 and dsr > 0.95 and cagr > bcagr and sh > bsh)
    print(f"\n[VERDICT] {'PASS — DL beats 0050 OOS on IC+CAGR+Sharpe, DSR-robust' if beats else 'FAIL — DL does NOT beat 0050 OOS (need pred-IC>0.03 AND DSR>0.95 AND CAGR>0050 AND Sharpe>0050)'}")
    if not beats:
        print("  Honest negative: a deep net on keyless TECHNICAL features (the only full-market keyless "
              "data) does not learn tradeable selection skill that beats passive 0050 out-of-sample — "
              "the signal isn't in the features (single-factor ICs all <0.05); DL amplifies noise it "
              "can't turn into alpha. (Chip/fundamental features that MIGHT add signal are paid/"
              "unavailable keyless for the full market.)")


if __name__ == "__main__":
    main()
