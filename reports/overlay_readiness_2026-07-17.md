# Overlay Backtest Readiness — 2026-07-17

**History:** 34 daily snapshot date(s) accrued in `docs/data/_overlay_history/`.
**Horizon gate:** 60 subsequent snapshot dates required to measure forward return.
**Min-fired floor:** 100 fired-with-horizon events (mirrors `run_backtest.FIRED_FLOOR`).
**Wilson-CI gate:** CI-lower > base rate (same gate as `run_backtest.main()`).

**Summary:** 0/35 signal families READY (35 accruing — not yet backtestable).

| signal-family | source | kind | fired-total | fired-w-horizon | hit-rate | wilson-ci-lower | base | READY? | verdict |
|---|---|---|---|---|---|---|---|---|---|
| HN 討論熱度 篇 / 分 / 留言 | hackernews | sentiment | 83 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 83 fired, not ready |
| AAPL Stock Bucks Tech Rout: Retail Calls It 'Safe  | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| AI Stocks Poised for Outperformance Over the Next  | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| Circle Gets Street Low Target as Stablecoin Compet | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| Driving the Agentic AI Era: MiTAC Computing Showca | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| Growing an Income Tree: From $ to $ | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| HSBC upgrades Apple to Buy sees "strong cycle ahea | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| Investors rotate within AI trade as slowing hypers | news | catalyst | 2 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 2 fired, not ready |
| Is Meta Building The Next AI Cloud Giant? Report S | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| Large Cap Stocks with Exciting Potential and We Br | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| MU SNDK INTC AMD Other Chip Stocks Extend Slide –  | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| Network School founder says immigration probe at t | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| Nvidia and AMD Investors Must Be Prepared for Aug | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| Press Releases You Need to See This Week Including | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| Social Buzz: Wallstreetbets Stocks Mostly Lower Pr | news | catalyst | 2 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 2 fired, not ready |
| Stock Futures Drop as Tech Selloff Gathers Pace | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| These Industrial Stocks Will Benefit From the Tril | news | catalyst | 2 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 2 fired, not ready |
| Wall Street Expects This IPO Stock to Jump Over th | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| 【量大強漲股整理】【台股大崩盤】台積電「利多出盡」?別逃!我只做這件事! | news | catalyst | 3 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 3 fired, not ready |
| 台股崩近 千點 外資殺出史上第一大賣超 億元 狂砍台積電千億元 | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| 改寫台股史上最大跌點!看懂無差別賣壓背後真相 這三大止穩訊號浮現才是真買點! | news | catalyst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| 新聞聲量 則 最高 來源確認 | news | sentiment | 8 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 8 fired, not ready |
| FTD 交割失敗偏高 連續 個交割日 | sec_ftd | chip | 39 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 39 fired, not ready |
| FTD 交割失敗偏高 連續 個交割日、累計 股交割失敗 | sec_ftd | chip | 97 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 97 fired, not ready |
| 大戶吸籌 | tdcc | chip | 76 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 76 fired, not ready |
| 大戶集中度 | tdcc | chip | 74 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 74 fired, not ready |
| 散戶化/出貨 | tdcc | chip | 76 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 76 fired, not ready |
| 融券餘額下降 回補 | twse_margin | chip | 100 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 100 fired, not ready |
| 融資餘額單日大增 | twse_margin | chip | 20 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 20 fired, not ready |
| PE / 殖利率 / PB | twse_pe | fundamental | 227 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 227 fired, not ready |
| 殖利率 / PB | twse_pe | fundamental | 2 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 2 fired, not ready |
| 🚫 處置股 第 次 處置期間： / / ～ / / | twse_punish | risk | 8 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 8 fired, not ready |
| 三大法人持平 股 | twse_t86 | inst | 1 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 1 fired, not ready |
| 三大法人買超 股 | twse_t86 | inst | 119 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 119 fired, not ready |
| 三大法人賣超 股 | twse_t86 | inst | 82 | 0 | n/a | n/a | 0.000 | NO | accruing — 34 days history, 82 fired, not ready |

---

> **Note:** Hit-rate and Wilson-CI are placeholders (0/n) until `fired_with_horizon >= 1` AND price follow-through data (close at `date + horizon`) is computable from the accrued snapshots.  The snapshots only store `close` at signal-fire date; forward close will be inferred once sufficient history exists.

*Generated: 2026-07-17T18:50:48Z UTC*
