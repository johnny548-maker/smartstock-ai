# -*- coding: utf-8 -*-
"""Rule-based Chinese commentary — NO LLM, NO API key.

Derives a multi-section 點評 from which scoring factors fired, and quotes the
ACTUAL stop-loss / target PRICE levels (user ask #3) when provided.
"""
from config import stock_name


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


def _levels_line(levels):
    if not levels:
        return "4. 停損與目標：建議停損 -7%，第一目標 +15~25%（依個人風險承受度調整）。"
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


def analyze_stock(stock, score, factors, sector=None, levels=None):
    """Return a multi-section 中文 commentary string."""
    factors = factors or {}
    lines = [f"📌 {stock_name(stock)}  | 動能分數 {score}"]

    reasons = [k for k, v in factors.items() if v > 0]
    lines.append("1. 投資理由：" + ("、".join(reasons) + "。" if reasons else "目前無明顯正向訊號。"))

    lines.append("2. 短中線觀點：" + _trend_view(factors))

    if score >= 70:
        entry = "可於回測 5 日線不破時分批進場。"
    elif score >= 40:
        entry = "建議觀望，待量價同步轉強再介入。"
    else:
        entry = "訊號偏弱，暫不建議進場。"
    lines.append("3. 進出場策略：" + entry)

    lines.append(_levels_line(levels))

    base_risk = "美債殖利率上行壓抑成長股估值；AI 族群短線易過熱。"
    risks = [k for k, v in factors.items() if v < 0]
    lines.append("5. 風險：" + (("、".join(risks) + "；") if risks else "") + base_risk)

    return "\n".join(lines)
