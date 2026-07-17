# -*- coding: utf-8 -*-
"""Rule-based Chinese commentary — NO LLM, NO API key.

Derives a multi-section 點評 from which scoring factors fired, and quotes the
ACTUAL stop-loss / target PRICE levels (user ask #3) when provided.

2026-07-17 audit remediation:
* Entry narrative thresholds now import config.SCORE_GREEN_MIN / SCORE_AMBER_MIN —
  the SAME cut-offs that drive the 燈號/建議買入 verdict. The old inline `>= 70`
  let a 75-point stock read 「觀望」(verdict) and 「可分批進場」(narrative) at once.
* The risk section is assembled from the stock's ACTUAL fired negative signals
  (+ ATR volatility when levels are in hand) — the old fixed macro line
  (美債殖利率/AI 族群) printed for every stock every day regardless of the actual
  TNX level or the stock's sector. Inputs this function cannot see (earnings
  proximity, beta) are simply NOT claimed — honest omission over fabrication.
* No levels → no price levels. The old default fabricated 「停損 -7%，目標 +15~25%」
  out of thin air.
"""
from config import stock_name, SCORE_GREEN_MIN, SCORE_AMBER_MIN


def _trend_view(factors):
    has_trend = "趨勢(MA5>MA20)" in factors
    has_mom = "動能(5日上漲)" in factors
    if has_trend and has_mom:
        return "均線多頭排列且短線動能延續，趨勢偏多。"
    if has_trend:
        return "中期均線翻多，但短線動能轉弱，留意是否回測支撐。"
    if has_mom:
        return "短線有反彈動能，惟中期均線尚未轉強，視為反彈而非反轉。"
    return "趨勢與動能皆偏弱，暫不具進場優勢。"


def _levels_line(levels, score=None):
    if not levels:
        # Audit finding 3: NEVER invent numbers when no computed levels exist.
        if score is not None and score >= SCORE_GREEN_MIN:
            return "4. 進出場價位：暫無足夠價格資料計算進出場價位，不提供估計值。"
        return "4. 進出場價位：未達買入級，未提供進出場價位。"
    # honest framing: ATR number is a trade-management bracket, NOT a forecast;
    # the price-target is a STRUCTURE-based BAND (range), caveat-stamped.
    bracket = levels.get("atr_bracket")
    if bracket is None:
        bracket = levels.get("target")
    line = (f"4. 進出場價位：進場 {levels['entry']}，"
            f"停損 {levels['stop']}（{levels['stop_pct']}%，波動 ATR {levels['atr_pct']}%）。")
    band = levels.get("target_band") or []
    if band:
        lo, hi = band[0], band[-1]
        rng = f"{lo}" if lo == hi else f"{lo}–{hi}"
        line += (f"\n   目標區間（技術投影，非預測，含倖存者偏差，僅供參考）：{rng}"
                 + (f"；測幅目標 {levels['measured_move']}" if levels.get("measured_move") else "")
                 + f"。技術停利位（交易管理，非目標價）{bracket}。")
    else:
        line += f" 目標／技術停利位 {bracket}（交易管理，非預測目標價）。"
    adv = []
    if levels.get("swing_stop"):
        adv.append(f"結構停損 {levels['swing_stop']}")
    if levels.get("chandelier"):
        adv.append(f"移動停損 {levels['chandelier']}（持有：突破跌破或移動停損觸及即出）")
    if levels.get("fib_targets"):
        adv.append("Fib 延伸 " + "/".join(str(t) for t in levels["fib_targets"]))
    if adv:
        line += "\n   進階：" + "；".join(adv) + "。"
    return line


# Negative-factor label token → per-signal risk explanation. Keyed on stable label
# substrings from strategy.score_stock so a label tweak degrades to bare-label listing
# (never a wrong claim). Signals NOT observable here (earnings window, beta) are
# intentionally absent — upstream overlays (earnings_guard) carry those on the card.
_RISK_NOTES = [
    ("RSI過熱", "短線指標過熱，追高風險大"),
    ("量價背離", "OBV 走弱於價格，留意主力出貨"),
    ("外資賣超", "外資近日調節，法人動向轉空"),
    ("籌碼分散", "籌碼轉散，法人持續調節"),
    ("遠離52週高", "距 52 週高點仍遠，上方套牢賣壓重"),
    ("相對弱勢", "走勢弱於大盤，資金未青睞"),
]
_ATR_HIGH_PCT = 4.0     # daily ATR% at/above this → flag volatility as a risk


def _risk_view(factors, levels):
    """Assemble the risk section from the stock's ACTUAL fired negative signals
    (+ high ATR when levels are available). Honest fallback when nothing fired —
    no canned macro narrative (audit finding 3)."""
    notes = []
    for label, pts in (factors or {}).items():
        if pts >= 0:
            continue
        note = next((n for token, n in _RISK_NOTES if token in label), None)
        notes.append(f"{label}——{note}" if note else label)
    atr = (levels or {}).get("atr_pct")
    if isinstance(atr, (int, float)) and atr >= _ATR_HIGH_PCT:
        notes.append(f"波動偏高（日 ATR {atr}%），部位宜小")
    if not notes:
        return "本股目前無明顯負向訊號；仍須留意大盤系統性波動。"
    return "；".join(notes) + "。"


def analyze_stock(stock, score, factors, sector=None, levels=None):
    """Return a multi-section 中文 commentary string."""
    factors = factors or {}
    lines = [f"📌 {stock_name(stock)}  | 動能分數 {score}"]

    reasons = [k for k, v in factors.items() if v > 0]
    lines.append("1. 投資理由：" + ("、".join(reasons) + "。" if reasons else "目前無明顯正向訊號。"))

    lines.append("2. 短中線觀點：" + _trend_view(factors))

    # Entry narrative bands == the 燈號 verdict bands (config, one source of truth):
    # ≥GREEN_MIN(🟢 建議買入) → entry language; AMBER band(🟡 觀望) → watch language;
    # below → no-entry. The old inline `>= 70` contradicted the ≥90 建議買入 verdict.
    if score >= SCORE_GREEN_MIN:
        entry = "可於回測 5 日線不破時分批進場。"
    elif score >= SCORE_AMBER_MIN:
        entry = "建議觀望，待量價同步轉強再介入。"
    else:
        entry = "訊號偏弱，暫不建議進場。"
    lines.append("3. 進出場策略：" + entry)

    lines.append(_levels_line(levels, score))

    lines.append("5. 風險：" + _risk_view(factors, levels))

    return "\n".join(lines)
