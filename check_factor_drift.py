# -*- coding: utf-8 -*-
"""Phase 4 — factor-IC drift monitor (HITL-gated, REPORTS only; NEVER flips a weight).

Run monthly after run_factor_ic refreshes docs/data/_factor_ic_state.json. A factor that crosses
the keep/demote boundary opens a GitHub issue for human review — the config change + golden
re-baseline + ADR still require human sign-off (.decisions/2026-06-22-scorer-15y-revalidation.md
and the scorer-flip protocol). CI must NOT auto-apply any weight change.

Boundary (mirrors the vol_stable two-metric rule):
  * KEPT reward factor → demote-candidate when rank_ic < 0 AND top-decile edge < 1.0 (no longer
    beats the universe — weak on BOTH metrics, like vol_stable was).
  * DEMOTED factor → recover-candidate when rank_ic > 0 AND edge >= recover threshold.
Weak IC alone never flags (a positive edge means it still beats the universe — the ADR rule).
"""
import json
import sys

EDGE_FLOOR = 1.0          # top-decile edge below this = no longer beats the universe
RECOVER_EDGE = 1.5        # a demoted factor recovering past this warrants reconsideration
ALERT_FILE = "docs/data/_factor_drift_alert.txt"

# instflow (institutional net-flow: inst_foreign_buy/sell + inst_trust_buy) is NOT computed by
# run_factor_ic.py — it lives in the separate aux-combo IC-screen pipeline (run_optimize.py /
# factor_panels_aux.py), which found it FAILED (rank-IC -0.0122, commit 01025ee4f) and demoted
# it 2026-08-03 (see .decisions/2026-08-03-instflow-demotion.md). Because it isn't one of
# run_factor_ic.FAMILY_TO_PTS's families, it would otherwise be invisible to this monitor
# entirely — registered here so (a) demoted_from_config() correctly reports it as demoted and
# (b) if a future run ever writes an "instflow" rank_ic/edge pair into families (e.g. from a
# re-gate of the live scorer's actual split formulation, merged into
# docs/data/_factor_ic_state.json), find_drift() has a feedback channel to flag recovery.
EXTRA_FAMILY_TO_PTS = {
    "instflow": ["inst_foreign_buy", "inst_foreign_sell", "inst_trust_buy"],
}


def find_drift(families, demoted, edge_floor=EDGE_FLOOR, recover_edge=RECOVER_EDGE):
    """Pure: {family: {rank_ic, edge}} + the set of currently-demoted families → list of boundary
    crossings. Empty list = no drift. Missing metrics are skipped (never fabricate a verdict)."""
    alerts = []
    for fam, m in (families or {}).items():
        ic = (m or {}).get("rank_ic")
        edge = (m or {}).get("edge")
        if ic is None or edge is None:
            continue
        if fam not in demoted and ic < 0 and edge < edge_floor:
            alerts.append({"family": fam, "kind": "kept→demote-candidate",
                           "rank_ic": ic, "edge": edge})
        elif fam in demoted and ic > 0 and edge >= recover_edge:
            alerts.append({"family": fam, "kind": "demoted→recover-candidate",
                           "rank_ic": ic, "edge": edge})
    return alerts


def demoted_from_config(factor_pts=None):
    """A family is currently DEMOTED iff all its FACTOR_PTS reward keys are 0 (penalty-only
    families with no reward key are never 'demoted'). Lazy import keeps find_drift dependency-free.
    Merges run_factor_ic.FAMILY_TO_PTS (base factors this module's own rank-IC pipeline covers)
    with EXTRA_FAMILY_TO_PTS (families demoted via a sibling pipeline, e.g. instflow via the
    aux-combo IC screen) so the watch-list isn't blind to demotions this module didn't compute."""
    from run_factor_ic import FAMILY_TO_PTS
    if factor_pts is None:
        from config import FACTOR_PTS as factor_pts
    family_to_pts = dict(FAMILY_TO_PTS)
    family_to_pts.update(EXTRA_FAMILY_TO_PTS)
    out = set()
    for fam, keys in family_to_pts.items():
        if keys and all(int(factor_pts.get(k, 0)) == 0 for k in keys):
            out.add(fam)
    return out


def main(path="docs/data/_factor_ic_state.json"):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("SKIP drift check (cannot read %s): %s" % (path, e))
        return []
    families = data.get("families") or {}
    alerts = find_drift(families, demoted_from_config())
    if alerts:
        print("DRIFT: %d factor(s) crossed the keep/demote boundary — HITL review needed:" % len(alerts))
        for a in alerts:
            print("  %-12s %s  rank_ic=%s edge=%s" % (a["family"], a["kind"], a["rank_ic"], a["edge"]))
        with open(ALERT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join("%s — %s (rank_ic=%s, edge=%s)"
                              % (a["family"], a["kind"], a["rank_ic"], a["edge"]) for a in alerts))
    else:
        print("No factor-IC drift across the keep/demote boundary (HITL not needed).")
    return alerts


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/data/_factor_ic_state.json")
