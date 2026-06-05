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
