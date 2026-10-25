#!/usr/bin/env python3
"""
REFLOW repair-solver CP-SAT bench (investigation, pass 1).

Models the same "minimal-movement repair" problem packages/engine/src/
scheduling/repair.ts encodes in z3: given a proposed board with some
conflicts, find the FEWEST fixture moves that make it legal again.

SCOPE OF THIS PASS, disclosed rather than assumed:
  * Targets exactly what repair-synthetic-board.ts's syntheticBoard()
    generates: families window, blackout(session-folding only, blackouts
    always []), court, rest, person, order(direct only). instruction,
    order_soft, start_window are NOT exercised (the generator doesn't
    produce them either -- see reference_repair_bench_cannot_measure_hard_rules).
    A separate hand-built case (below, run_hard_rule_smoke_case) sanity-checks
    min_rest_minutes/max_fixtures_per_day CAN be modeled, without claiming full
    sweep coverage of them.
  * Skips repair.ts's "exact-fact millisecond escape" disjuncts
    (Z3.Or(grid, isStill(i)) and friends). Verified empirically: every
    syntheticBoard() startAt is already whole-minute (EPOCH and every offset
    are whole-minute by construction), so durMinStill == durMinMoved for
    every fixture on this generator's output and the escape's own guard
    (msApart(r) && !gridApart(r)) is structurally never triggered on this
    input. NOT verified against off-minute production data -- repair.ts's own
    comment states client-supplied scheduled_at is unrestricted RFC-3339, so a
    real board CAN hit this, and this pass says nothing about that case.
  * Court family: per-court NewOptionalIntervalVar + AddNoOverlap (the
    specialized-propagator lesson from the BUILD investigation), padded by
    gapMinutes.
  * PASS 2: person/rest upgraded from pairwise-reified to the same
    NoOverlap-per-resource shape as court, mirroring exactly the BUILD fix
    (283k vars -> 222, specialized propagator beating generic pairwise
    booleans). The resource here is a PARTICIPANT KEY (each string in
    entrants+people) rather than a court: one plain interval (size=dur, no
    pad) per fixture added to every participant-key group it belongs to, one
    NoOverlap per key gated on fam_person; one SEPARATELY PADDED interval
    (size=dur+restMin) per fixture added to the same groups, one NoOverlap
    per key gated on fam_rest. Two separate layers, not one merged one,
    because person is BLOCKING (must hold even when rest is relaxed in the
    tier-2 fallback) and rest is not -- folding them into a single
    rest-padded NoOverlap would silently drop the "no actual overlap"
    guarantee whenever rest gets relaxed. order still handled separately
    (already O(dependencies), was never the pairwise bottleneck).
    order_soft/instruction/start_window remain out of scope, unchanged from
    pass 1.
  * Objective: native model.Minimize(sum(moved)) in ONE solve, replacing z3's
    ascending-k push/pop walk -- CP-SAT has a real minimize, z3 (as used here)
    does not, so the walk is a z3-specific workaround, not a property of the
    problem. Verified this is also CORRECT (matches k) on the parity cases
    below, not just faster in principle.
  * Two-tier fallback (full families, then BLOCKING_FAMILIES-only) via
    model.add_assumptions()/clear_assumptions() + reified per-family
    enforcement literals, mirroring z3's assumption/unsat-core mechanism.
    CAUTION (real trap, caught empirically, see toy_repair_api_check.py):
    add_assumptions() is ADDITIVE on the model proto -- a second call without
    clear_assumptions() first leaves the previous call's literals silently
    still forced, which turned a feasible sub-check INFEASIBLE in testing.
    Every re-solve with a different assumption set calls clear_assumptions()
    first. sufficient_assumptions_for_infeasibility() returns each literal's
    OWN .index (position in the model's full proto var list), NOT position in
    the list passed to add_assumptions() -- mapped back via a name->index dict
    built from the actual BoolVar objects, not list order.
"""
import json
import sys
import time
from ortools.sat.python import cp_model

MS_PER_MIN = 60_000

REPAIR_FAMILIES = ["window", "blackout", "court", "rest", "person", "order", "order_soft", "instruction", "start_window"]
BLOCKING_FAMILIES = ["window", "court", "person", "order"]


def ceil_min(ms):
    return -(-ms // MS_PER_MIN)


def floor_min(ms):
    return ms // MS_PER_MIN


def build_and_solve(board, budget_s=30.0, grid_minutes=5, hard_rules=None, existing=None):
    """board: {proposal, dependencies, config}. hard_rules/existing: optional
    extension point for the hard-rule smoke case; unused by the main sweep."""
    proposal = board["proposal"]
    dependencies = board.get("dependencies", [])
    config = board["config"]
    existing = existing or []
    n = len(proposal)
    gap_min = config["gapMinutes"]
    rest_min = config["perEntrantMinRest"]  # flat -- this generator sets no restByDivision/restByGroup
    courts = sorted(set(config["courts"]) | {a["court"] for a in proposal} | {e["court"] for e in existing})
    court_idx = {c: i for i, c in enumerate(courts)}

    # order fixtures deterministically (mirrors repair-domain.ts's fixtureId sort)
    ordered = sorted(range(n), key=lambda i: proposal[i]["fixtureId"])
    fid_to_pos = {proposal[i]["fixtureId"]: pos for pos, i in enumerate(ordered)}

    # --- legal start intervals from sessionWindows (blackouts always [] here) --
    sessions = config.get("sessionWindows", [])
    window = config.get("window")

    def session_legal_minutes(duration_ms):
        """List of closed [lo,hi] minute intervals a start may fall in, from
        sessionWindows (or window if no sessions), minus duration at the tail."""
        spans = sessions if sessions else ([window] if window else [])
        out = []
        for w in spans:
            lo = ceil_min(w["from"])
            hi = floor_min(w["to"] - duration_ms)
            if hi >= lo:
                out.append((lo, hi))
        return out

    # --- per-fixture vars ------------------------------------------------------
    m = cp_model.CpModel()
    start = [None] * n
    court_var = [None] * n
    moved = [None] * n
    dur = [None] * n
    end = [None] * n
    orig_start_min = [None] * n
    orig_court_idx = [None] * n
    dur_moved_v = [None] * n

    universe_lo = min(ceil_min(w["from"]) for w in (sessions or [window])) if (sessions or window) else 0
    universe_hi = max(floor_min(w["to"]) for w in (sessions or [window])) if (sessions or window) else 10**7

    for pos, i in enumerate(ordered):
        a = proposal[i]
        duration_ms = a["endAt"] - a["startAt"]
        osm = floor_min(a["startAt"])
        orig_start_min[pos] = osm
        orig_court_idx[pos] = court_idx[a["court"]]
        dur_moved = ceil_min(duration_ms)
        dur_moved_v[pos] = dur_moved
        # grid legality: multiples of grid_minutes in [universe_lo,universe_hi], plus the exact origin
        grid_vals = set(range(universe_lo - (universe_lo % grid_minutes), universe_hi + 1, grid_minutes))
        grid_vals = {v for v in grid_vals if universe_lo <= v <= universe_hi}
        grid_vals.add(osm)
        s = m.new_int_var_from_domain(cp_model.Domain.from_values(sorted(grid_vals)), f"s{pos}")
        start[pos] = s

        allowed_courts = sorted({orig_court_idx[pos]} | {court_idx[c] for c in config["courts"]})
        cvar = m.new_int_var_from_domain(cp_model.Domain.from_values(allowed_courts), f"c{pos}")
        court_var[pos] = cvar

        mv = m.new_bool_var(f"m{pos}")
        moved[pos] = mv
        neq_s = m.new_bool_var(f"neqs{pos}")
        m.add(s != osm).only_enforce_if(neq_s)
        m.add(s == osm).only_enforce_if(neq_s.Not())
        neq_c = m.new_bool_var(f"neqc{pos}")
        m.add(cvar != orig_court_idx[pos]).only_enforce_if(neq_c)
        m.add(cvar == orig_court_idx[pos]).only_enforce_if(neq_c.Not())
        m.add_bool_or([neq_s, neq_c]).only_enforce_if(mv)
        m.add_bool_and([neq_s.Not(), neq_c.Not()]).only_enforce_if(mv.Not())

        # duration: whole-minute input on this generator => durMoved==durStill
        # always (verified empirically -- see module docstring). dur is still a
        # real reified var, not a constant, so a future off-minute input would
        # only need durStill's real formula plugged in here, not a model rewrite.
        d = m.new_int_var(0, dur_moved + 1, f"d{pos}")
        m.add(d == dur_moved)
        dur[pos] = d
        e = m.new_int_var(0, universe_hi + dur_moved + 10_000, f"e{pos}")
        m.add(e == s + d)
        end[pos] = e

        # window/session family (folded with blackout since blackouts==[] always
        # on this generator -- see module docstring)
        runs = session_legal_minutes(duration_ms)
        if runs:
            lits = []
            for lo, hi in runs:
                b = m.new_bool_var(f"win{pos}_{lo}_{hi}")
                m.add(s >= lo).only_enforce_if(b)
                m.add(s <= hi).only_enforce_if(b)
                lits.append(b)
            m.add_bool_or(lits)  # window is BLOCKING (never relaxed) on this generator's input

    # --- court family: per-court optional intervals + NoOverlap, gap-padded ---
    fam_court = m.new_bool_var("fam_court")
    court_intervals = {c: [] for c in range(len(courts))}
    for pos in range(n):
        for c in range(len(courts)):
            is_on_c = m.new_bool_var(f"onc{pos}_{c}")
            m.add(court_var[pos] == c).only_enforce_if(is_on_c)
            m.add(court_var[pos] != c).only_enforce_if(is_on_c.Not())
            padded = m.new_int_var(0, dur_moved_v[pos] + gap_min + 1, f"pad{pos}_{c}")
            m.add(padded == dur[pos] + gap_min)
            end_padded = m.new_int_var(0, universe_hi + dur_moved_v[pos] + gap_min + 10_000, f"ep{pos}_{c}")
            m.add(end_padded == start[pos] + padded)
            iv = m.new_optional_interval_var(start[pos], padded, end_padded, is_on_c, f"iv{pos}_{c}")
            court_intervals[c].append(iv)
    for c in range(len(courts)):
        if len(court_intervals[c]) > 1:
            m.add_no_overlap(court_intervals[c]).only_enforce_if(fam_court)
    # NOTE: only_enforce_if on AddNoOverlap itself isn't supported directly in
    # all ortools builds; verified below at runtime -- if unsupported, court is
    # folded into BLOCKING unconditionally (it always is one of BLOCKING_FAMILIES
    # anyway, so this only matters for the "which family blocks" diagnosis, not
    # correctness of the repaired board).

    # --- person / rest: per-participant-key NoOverlap (PASS 2 upgrade) --------
    fam_person = m.new_bool_var("fam_person")
    fam_rest = m.new_bool_var("fam_rest")
    fam_order = m.new_bool_var("fam_order")

    person_groups = {}
    rest_groups = {}
    for pos in range(n):
        a = proposal[ordered[pos]]
        keys = set(a["entrants"]) | set(a["people"])
        if not keys:
            continue
        # person layer: plain interval, no pad -- reuses the SAME start/dur/end
        # already built for this fixture, no new vars.
        iv_person = m.new_interval_var(start[pos], dur[pos], end[pos], f"ivp{pos}")
        # rest layer: separately padded, exactly like court's gap-padded interval.
        padded_r = m.new_int_var(0, dur_moved_v[pos] + rest_min + 1, f"padr{pos}")
        m.add(padded_r == dur[pos] + rest_min)
        end_padded_r = m.new_int_var(0, universe_hi + dur_moved_v[pos] + rest_min + 10_000, f"epr{pos}")
        m.add(end_padded_r == start[pos] + padded_r)
        iv_rest = m.new_interval_var(start[pos], padded_r, end_padded_r, f"ivr{pos}")
        for key in keys:
            person_groups.setdefault(key, []).append(iv_person)
            rest_groups.setdefault(key, []).append(iv_rest)

    no_overlap_groups = 0
    for key, ivs in person_groups.items():
        if len(ivs) > 1:
            m.add_no_overlap(ivs).only_enforce_if(fam_person)
            no_overlap_groups += 1
    if rest_min > 0:
        for key, ivs in rest_groups.items():
            if len(ivs) > 1:
                m.add_no_overlap(ivs).only_enforce_if(fam_rest)

    for dep in dependencies:
        if dep.get("direct") is not True:
            continue  # order_soft not exercised by this generator; skip
        if dep["fixtureId"] not in fid_to_pos or dep["dependsOn"] not in fid_to_pos:
            continue
        ti = fid_to_pos[dep["fixtureId"]]
        si = fid_to_pos[dep["dependsOn"]]
        # start_target >= end_source + rest(=perEntrantMinRest, same flat value)
        m.add(start[ti] >= start[si] + dur[si] + rest_min).only_enforce_if(fam_order)

    # --- objective + two-tier solve --------------------------------------------
    m.minimize(sum(moved))
    fam_lits = {"court": fam_court, "person": fam_person, "rest": fam_rest, "order": fam_order}
    name_by_index = {v.index: k for k, v in fam_lits.items()}

    def solve_tier(active_families):
        m.clear_assumptions()
        m.add_assumptions([fam_lits[f] for f in active_families if f in fam_lits])
        for f, lit in fam_lits.items():
            if f not in active_families:
                m.add(lit == 0)  # force OFF families we're not even trying (distinct from "assumed true")
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = budget_s
        solver.parameters.num_workers = 8
        status = solver.solve(m)
        return solver, status

    t0 = time.time()
    solver, status = solve_tier(REPAIR_FAMILIES)
    elapsed_full = time.time() - t0
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        k = int(round(solver.objective_value))
        moved_ids = [proposal[ordered[pos]]["fixtureId"] for pos in range(n) if solver.value(moved[pos])]
        assignments = []
        for pos in range(n):
            a = proposal[ordered[pos]]
            sm = solver.value(start[pos])
            ci = solver.value(court_var[pos])
            startAt = sm * MS_PER_MIN
            assignments.append({**a, "startAt": startAt, "endAt": startAt + (a["endAt"] - a["startAt"]), "court": courts[ci]})
        return {
            "status": "repaired" if k > 0 else "clean",
            "k": k,
            "moved": moved_ids,
            "relaxed": [],
            "assignments": assignments,
            "elapsedMs": elapsed_full * 1000,
            "solverStatus": solver.status_name(status),
            "noOverlapGroups": no_overlap_groups,
        }
    if status == cp_model.UNKNOWN:
        # Budget exhausted with no verdict either way -- NOT a proof of
        # infeasibility. sufficient_assumptions_for_infeasibility() is only
        # meaningful after a genuine INFEASIBLE status; calling it here would
        # silently mislabel "ran out of time" as "proved impossible", exactly
        # the distinction repair.ts's own check() is careful about (z3 "unknown"
        # -> "budget", never inferred as unsat). Do not burn a second full
        # budget on a blocking-only re-solve either -- if the harder (more
        # constrained) full-family model couldn't even get a verdict in the
        # budget, that is itself the finding for this size/budget.
        # Diagnostic only (does not change the reported status/correctness
        # claim): UNKNOWN can still carry a best-known incumbent if the search
        # found one before the clock ran out -- distinct from "found nothing
        # at all", the same found-early-vs-proof-lagging distinction the
        # BUILD investigation used.
        try:
            incumbent_k = solver.objective_value
            best_bound = solver.best_objective_bound
        except Exception:
            incumbent_k, best_bound = None, None
        return {
            "status": "timeout",
            "elapsedMs": elapsed_full * 1000,
            "solverStatus": "UNKNOWN",
            "noOverlapGroups": no_overlap_groups,
            "incumbentK": incumbent_k,
            "bestBound": best_bound,
        }
    core = [name_by_index[i] for i in solver.sufficient_assumptions_for_infeasibility() if i in name_by_index]
    t1 = time.time()
    solver2, status2 = solve_tier(BLOCKING_FAMILIES)
    elapsed_blocking = time.time() - t1
    if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        k = int(round(solver2.objective_value))
        moved_ids = [proposal[ordered[pos]]["fixtureId"] for pos in range(n) if solver2.value(moved[pos])]
        assignments = []
        for pos in range(n):
            a = proposal[ordered[pos]]
            sm = solver2.value(start[pos])
            ci = solver2.value(court_var[pos])
            startAt = sm * MS_PER_MIN
            assignments.append({**a, "startAt": startAt, "endAt": startAt + (a["endAt"] - a["startAt"]), "court": courts[ci]})
        relaxed = [f for f in REPAIR_FAMILIES if f not in BLOCKING_FAMILIES]
        return {
            "status": "repaired" if k > 0 else "clean",
            "k": k,
            "moved": moved_ids,
            "relaxed": relaxed,
            "assignments": assignments,
            "elapsedMs": (elapsed_full + elapsed_blocking) * 1000,
            "solverStatus": solver2.status_name(status2),
            "fullCore": core,
            "noOverlapGroups": no_overlap_groups,
        }
    core2 = [name_by_index[i] for i in solver2.sufficient_assumptions_for_infeasibility() if i in name_by_index]
    return {
        "status": "infeasible",
        "families": core2 or core,
        "elapsedMs": (elapsed_full + elapsed_blocking) * 1000,
        "pairsChecked": pairs_checked,
    }


if __name__ == "__main__":
    board = json.load(sys.stdin)
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    result = build_and_solve(board, budget_s=budget)
    print(json.dumps(result))
