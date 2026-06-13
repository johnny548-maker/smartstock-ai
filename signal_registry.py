# -*- coding: utf-8 -*-
"""B1 — single source of truth for the LEADERSHIP scoring signals.

The 7 backtest-gated leadership signals were defined in THREE shapes: the scored if-blocks in
strategy.score_stock, the (s,b)->bool lambdas in run_backtest.DEFS, and the labels in verdict.
This registry is the canonical spec — name + config weight attr + the scored factor label +
the predicate — so the scorer and the backtest cannot drift apart (a test asserts the registry
predicates equal the same underlying signal funcs the backtest uses).

Each `fires(df, bench, setup)` reads `setup` (= technical_setup.analyze_setup(df), passed in so
the bundled patterns are computed once) for the technical patterns, or the OHLCV directly for
the volume/RS signals. OVERLAY-NOT-SCORER unaffected — these are score factors gated by
config.LEAD_* weights (0 = demoted, never added).
"""
from collections import namedtuple

from volume_signals import accumulating as _accumulating, vdu_thrust as _vdu_thrust
from signals import rs_line_new_high as _rs_line_new_high

LeadershipSignal = namedtuple("LeadershipSignal", "name weight_attr label fires")

# Order + labels + weight attrs are byte-identical to the old strategy.score_stock if-blocks.
LEADERSHIP = [
    LeadershipSignal("first_new_high", "LEAD_FIRST_NEW_HIGH", "久盤後首次新高(回測lift0.68)",
                     lambda df, bench, setup: setup["first_new_high"]),
    LeadershipSignal("power_pivot", "LEAD_POWER_PIVOT", "Power pivot放量突破(回測lift1.24)",
                     lambda df, bench, setup: setup["power_pivot"]),
    LeadershipSignal("stage2", "LEAD_STAGE2", "Stage2上升趨勢(回測lift1.00)",
                     lambda df, bench, setup: setup["stage2"]),
    LeadershipSignal("pocket_pivot", "LEAD_POCKET_PIVOT", "Pocket pivot吸籌(回測lift0.99)",
                     lambda df, bench, setup: setup["pocket_pivot"]),
    LeadershipSignal("ud_accum", "LEAD_UD_ACCUM", "U/D量吸籌(回測lift1.55)",
                     lambda df, bench, setup: _accumulating(df)),
    LeadershipSignal("vdu_thrust", "LEAD_VDU_THRUST", "VDU→Thrust噴出(回測lift1.61)",
                     lambda df, bench, setup: _vdu_thrust(df)),
    LeadershipSignal("rs_new_high", "LEAD_RS_NEW_HIGH", "RS線新高領先(回測lift0.99)",
                     lambda df, bench, setup: bench is not None and _rs_line_new_high(df, bench)),
]
