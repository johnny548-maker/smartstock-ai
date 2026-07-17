# Overlay Backtest Readiness — 2026-06-07

**History:** 1 daily snapshot date(s) accrued in `docs/data/_overlay_history/`.
**Horizon gate:** 60 subsequent snapshot dates required to measure forward return.
**Min-fired floor:** 100 fired-with-horizon events (mirrors `run_backtest.FIRED_FLOOR`).
**Wilson-CI gate:** CI-lower > base rate (same gate as `run_backtest.main()`).

**Summary:** 0/9 signal families READY (9 accruing — not yet backtestable).

| signal-family | source | kind | fired-total | fired-w-horizon | hit-rate | wilson-ci-lower | base | READY? | verdict |
|---|---|---|---|---|---|---|---|---|---|
| HN 討論熱度 篇 / 分 / 留言 | hackernews | sentiment | 4 | 0 | n/a | n/a | 0.000 | NO | accruing — 1 days history, 4 fired, not ready |
| FTD 交割失敗偏高 連續 個交割日 | sec_ftd | chip | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 1 days history, 1 fired, not ready |
| FTD 交割失敗偏高 連續 個交割日、累計 股交割失敗 | sec_ftd | chip | 4 | 0 | n/a | n/a | 0.000 | NO | accruing — 1 days history, 4 fired, not ready |
| 大戶集中度 | tdcc | chip | 7 | 0 | n/a | n/a | 0.000 | NO | accruing — 1 days history, 7 fired, not ready |
| 融券餘額下降 回補 | twse_margin | chip | 2 | 0 | n/a | n/a | 0.000 | NO | accruing — 1 days history, 2 fired, not ready |
| 融資餘額單日大增 | twse_margin | chip | 2 | 0 | n/a | n/a | 0.000 | NO | accruing — 1 days history, 2 fired, not ready |
| PE / 殖利率 / PB | twse_pe | fundamental | 7 | 0 | n/a | n/a | 0.000 | NO | accruing — 1 days history, 7 fired, not ready |
| 三大法人買超 股 | twse_t86 | inst | 3 | 0 | n/a | n/a | 0.000 | NO | accruing — 1 days history, 3 fired, not ready |
| 三大法人賣超 股 | twse_t86 | inst | 4 | 0 | n/a | n/a | 0.000 | NO | accruing — 1 days history, 4 fired, not ready |

---

> **Note:** Hit-rate and Wilson-CI are placeholders (0/n) until `fired_with_horizon >= 1` AND price follow-through data (close at `date + horizon`) is computable from the accrued snapshots.  The snapshots only store `close` at signal-fire date; forward close will be inferred once sufficient history exists.

*Generated: 2026-06-07T05:45:59Z UTC*
