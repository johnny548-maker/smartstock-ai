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
    return (f"4. 進出場價位：參考進場 {levels['entry']}，"
            f"停損 {levels['stop']}（{levels['stop_pct']}%），"
            f"目標 {levels['target']}（+{levels['target_pct']}%），"
            f"R/R {levels['rr']}:1（波動 ATR {levels['atr_pct']}%）。")


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
