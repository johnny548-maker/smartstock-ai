# -*- coding: utf-8 -*-
"""Pure-OHLCV volume/accumulation signals — work on ANY market (no 法人 data).

The existing quiet_accumulation depends on TWSE T86 法人 data, so it is a silent
no-op for every US name — yet AAOI/NVTS-class small-caps are exactly where an
accumulation footprint front-runs the move. These signals read it straight from
price+volume, keyless, deterministic, and orthogonal to the trend factors.

  • Volume Dry-Up (VDU): recent volume contracts well below its base — the quiet
    before institutions push. Wyckoff/Minervini's "no supply" tell.
  • VDU → Thrust: a VDU base followed by an up-day on a volume expansion.
  • Up/Down volume ratio: Σ up-day volume ÷ Σ down-day volume over a window;
    >1 means more volume on advances than declines = accumulation.
"""

VDU_RECENT = 10            # bars in the 'recent' dry-up window
VDU_BASE = 50             # bars in the baseline window
VDU_RATIO = 0.65          # recent avg vol < 65% of base avg → dried up
THRUST_VOL_MULT = 1.5     # up-day volume > 1.5× recent dried baseline → thrust
UD_WINDOW = 50            # up/down volume ratio window
UD_ACCUM = 1.10           # ratio above this = accumulation

# A/D grade thresholds (ratio cut points; backtest can sweep these)
AD_A = 1.50               # r ≥ → A 重度吸籌
AD_B = 1.15               # AD_A > r ≥ → B 溫和吸籌
AD_C_LO = 0.85            # AD_B > r ≥ → C 中性
AD_D_LO = 0.65            # AD_C_LO > r ≥ → D 溫和派發 ; r < → E 重度派發

AD_WINDOW = 65            # ~13 trading weeks
AD_HALF_LIFE = 25         # weight halves every ~5 weeks (recency emphasis)


def volume_dry_up(df, recent=VDU_RECENT, base=VDU_BASE, ratio=VDU_RATIO):
    """True when recent avg volume has contracted below `ratio`× the base avg."""
    if df is None or len(df) < base + 1:
        return False
    vol = df["Volume"]
    recent_avg = vol.iloc[-recent:].mean()
    base_avg = vol.iloc[-base:].mean()
    return bool(base_avg) and recent_avg < base_avg * ratio


def vdu_thrust(df, recent=VDU_RECENT, base=VDU_BASE, ratio=VDU_RATIO,
               vol_mult=THRUST_VOL_MULT):
    """A volume-dry-up base resolved by an up-day on expanding volume — the entry
    trigger. Checks the dry-up on the bars BEFORE today, then today = up + volume
    surge vs that dried baseline."""
    if df is None or len(df) < base + 2:
        return False
    close, vol = df["Close"], df["Volume"]
    prior = df.iloc[:-1]
    if not volume_dry_up(prior, recent, base, ratio):
        return False
    up = close.iloc[-1] > close.iloc[-2]
    dried_avg = vol.iloc[-recent - 1:-1].mean()
    surge = bool(dried_avg) and vol.iloc[-1] > dried_avg * vol_mult
    return bool(up and surge)


def up_down_volume_ratio(df, window=UD_WINDOW):
    """Σ(up-day volume) / Σ(down-day volume) over `window`. None if no down days."""
    if df is None or len(df) < window + 1:
        return None
    close, vol = df["Close"], df["Volume"]
    chg = close.diff()
    up_vol = vol[chg > 0].iloc[-window:].sum()
    dn_vol = vol[chg < 0].iloc[-window:].sum()
    if not dn_vol:
        return None
    return float(up_vol / dn_vol)


def accumulating(df, window=UD_WINDOW, threshold=UD_ACCUM):
    """True when up/down volume ratio signals accumulation."""
    r = up_down_volume_ratio(df, window)
    return r is not None and r >= threshold


def weighted_up_down_volume_ratio(df, window=AD_WINDOW, half_life=AD_HALF_LIFE):
    """Recency-weighted Σ(w·up-vol) / Σ(w·down-vol) over the last `window` bars.

    Weight w_i = 0.5**((window-1-i)/half_life) so the most-recent bar weighs 1.0
    and older bars decay with a `half_life`-bar half-life — recent accumulation/
    distribution dominates, distinguishing this from the flat up_down_volume_ratio.
    Unchanged days (chg==0) are excluded from BOTH sums. Returns None if the frame
    is too short (need window+1 for the diff), has no 'Volume', or the weighted
    down-volume sum is 0 (avoids ZeroDivision on a strictly monotonic-up run).
    """
    if df is None or "Volume" not in df or len(df) < window + 1:
        return None
    close = df["Close"]
    vol = df["Volume"]
    chg = close.diff().iloc[-window:].to_numpy(float)
    v = vol.iloc[-window:].to_numpy(float)
    up_sum = 0.0
    dn_sum = 0.0
    for i in range(window):
        w = 0.5 ** ((window - 1 - i) / half_life)
        if chg[i] > 0:
            up_sum += w * v[i]
        elif chg[i] < 0:
            dn_sum += w * v[i]
    if dn_sum == 0:
        return None
    return float(up_sum / dn_sum)


_AD_LABELS = {
    "A": "重度吸籌", "B": "溫和吸籌", "C": "中性",
    "D": "溫和派發", "E": "重度派發",
}


def acc_dist_grade(df, window=AD_WINDOW, half_life=AD_HALF_LIFE):
    """Honest ANALOG of IBD's Accumulation/Distribution A-E rating (the exact A/D
    algorithm is proprietary). Grades a name on a recency-weighted up/down volume
    ratio — A/B = institutions buying (吸籌), D/E = distributing (派發), C = neutral.

    OVERLAY-NOT-SCORER: this is an INFORMATIONAL badge only. It rides the same
    informational-overlay rail as earnings/liquidity (card-only) and MUST NOT enter
    strategy.score_stock or any factor weight — purely a glance-able overlay.

    Returns {'grade','ratio','label','bullish'} or None when the weighted ratio is
    None (too short / no volume / no down-volume). Mapping by ratio r:
      r≥AD_A→A, AD_A>r≥AD_B→B, AD_B>r≥AD_C_LO→C, AD_C_LO>r≥AD_D_LO→D, r<AD_D_LO→E.
    bullish = grade in ('A','B').
    """
    r = weighted_up_down_volume_ratio(df, window, half_life)
    if r is None:
        return None
    if r >= AD_A:
        grade = "A"
    elif r >= AD_B:
        grade = "B"
    elif r >= AD_C_LO:
        grade = "C"
    elif r >= AD_D_LO:
        grade = "D"
    else:
        grade = "E"
    return {
        "grade": grade,
        "ratio": round(r, 2),
        "label": _AD_LABELS[grade],
        "bullish": grade in ("A", "B"),
    }
