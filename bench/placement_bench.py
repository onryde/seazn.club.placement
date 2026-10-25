#!/usr/bin/env python3
"""
Standalone CP-SAT model of the same fixture-scheduling problem the repo's z3
build solver encodes (packages/engine/src/scheduling/build-encode.ts and
build.ts), run across a benchmark sweep comparable to
packages/engine/scripts/bench-build.ts.

RESEARCH HARNESS. This SWEEP is throwaway and is not wired into build.ts or
any test — but `placement_bench_boards.py` beside it NO LONGER IS. That module
was extracted from this file and is now imported by the service's own test
suite (`tests/test_model.py`, via
`production_board()`), so **deleting or editing it changes what the service
is tested against.** Treat this file as disposable; treat that one as load
bearing.

The sweep exists only to answer: does Google OR-Tools CP-SAT clear
meaningfully larger boards than z3 within the same wall-clock budget, once
the FULL constraint family set and the T0-T3 lexicographic objective are
both in play (not just T0 placement, which the first pass of this script
proved out alone)?

--- board generation (mirrors packages/engine/scripts/bench-build.ts) -------

  THE CODE described in this section now lives in `placement_bench_boards.py` and
  is imported back below — moved, not changed, so the service's own tests can
  drive the identical generator instead of a second copy of it. This section
  still documents it because the reasoning belongs with the model it feeds.

  * matchMinutes=40, gapMinutes=10 (see "court-turnaround gap" below for why
    10 and not 0 now). UNLIKE THE FIRST PASS, this script DOES re-space the
    lattice onto `gcd(40,10)=10`-minute ticks within each day
    (`build-grid.ts:gridStepMinutes` does the same, for the same reason) —
    an early version of this sweep kept the coarse 40-minute grid from the
    first pass and got it WRONG: with 40-minute-spaced candidate starts and
    a 50-minute (`matchMinutes+gapMinutes`) turnaround, `windowsOver`
    produces AtMostOne pairs over every adjacent tick, which allows at most
    every OTHER 40-minute tick — roughly HALVING each court-day's usable
    capacity (measured: 140-fixture board placed only 69/140, OPTIMAL, i.e.
    PROVEN, not budget-starved). That is not what gapMinutes=10 actually
    costs in production: the true per-court-day ceiling at a 50-minute
    turnaround over a 720-minute session is `floor((720-40)/50)+1 = 14`
    matches (≈78% of the gap=0 ceiling of 18), not ≈50%. The 50% figure was
    an artifact of only offering the solver 40-minute-multiple candidate
    starts instead of the 50-minute-multiple ones it actually wants — fixed
    by generating every 10-minute tick within the SAME per-day time span the
    first pass used (`spcd * matchMinutes` minutes), i.e. `spcd * 4` ticks
    per court-day instead of `spcd`. This is a real, reportable side effect
    of modeling gapMinutes correctly: `real_slots` (and therefore the model
    size) is ~4x what the first pass's board of the same `target_slots`
    label had, at every point in this sweep.
  * a session window per day, 8:00-20:00 local (12h = 720min -> 18 slots per
    court per day at the 40-minute step, same SLOTS_PER_COURT_DAY as before).
  * entrants assigned round-robin from a pool sized so every entrant plays
    exactly `per_entrant` matches (2, matching the R18 knee sweep's
    `--per-entrant=2` runs).
  * two divisions, alternating by fixture index.
  * `min_rest_minutes` = 80 min at competition scope (raises the config's own
    `perEntrantMinRest` = 40, exactly as bench-build.ts's default
    `--hard-rest=80` does).
  * `max_fixtures_per_day` per division, derived as
    ceil(n / divisions / days) when not overridden.
  * `notAfter` pins the two highest-indexed entrants to the very first slot
    time, matching the R18 knee sweep's `--not-after=2` runs.

--- constraint families modeled (from build-encode.ts) ----------------------

  section 1 (build-encode.ts:181): AtMostOne over a fixture's own slots, plus
    placed[i] defined as "this fixture landed somewhere".
  section 2 (build-encode.ts:190): AtMostOne over a slot's fixtures — court
    exclusivity's other half (exact concurrency, distinct from turnaround).
  section 3 (build-encode.ts:196, turnaround gap) — NOW MODELED, not skipped.
    `gapMinutes=10` makes the window width (matchMinutes+gapMinutes=50min)
    wider than the 10-minute lattice tick (see the lattice re-spacing note
    above), so several consecutive same-court ticks DO fall inside one
    window and this family is no longer a no-op the way it was at
    gapMinutes=0. Built exactly as z3 does: an occupancy literal
    per slot (`occ_any`, "some fixture occupies slot s" — exact because
    section 2 already caps a slot's occupancy at one), and one AtMostOne per
    `windowsOver` run of same-court slots at width `matchMinutes+gapMinutes`.
    THIS IS A DIFFERENT RULE FROM PARTICIPANT REST (section 6): section 3 is
    "no two fixtures — regardless of participants — may start this close
    together on the SAME court"; section 6 is "the SAME participant may not
    play again this soon, on ANY court." A pair that shares no participant
    can still trip section 3 if both land on the same court within the gap;
    a pair sharing a participant can trip section 6 across two DIFFERENT
    courts where section 3 says nothing at all.
  section 5 (build-encode.ts:220, start windows): the notAfter pin — a
    restricted domain (only first-tick slots admitted for pinned fixtures).
  section 6 (build-encode.ts:228, participant rest): "at most one placement,
    among a participant's fixtures, inside any (duration+rest) sliding
    window" via the same `windows_over()` walk build-encode.ts uses. Entrant
    groups only (see the ORIGINAL simplification note below — still true,
    unchanged).
  section 7 (build-encode.ts:304, existing/immovable rows) — NOW MODELED.
    A handful of PINNED rows per board (see `existing_rows_for`) that the
    solver may never move, but whose court time AND participants still bind
    every movable fixture. Ported faithfully, including the two things a
    naive read of the names gets wrong:
      * the court-clash half uses `gapMinutes` (turnaround), NOT the
        participant rest bound — an existing row with NO shared participant
        still forbids nearby same-court placements, purely on gap grounds.
      * the rest half is ONE-DIRECTIONAL: `pairRest(movable, immovable)`,
        never the max of both directions the movable-movable case (section
        6) uses. z3's own comment is explicit about why: an `existing` row
        is never the "outer" side `validateAssignments` judges a pair from,
        so judging it both ways would refuse boards the verifier accepts.
    `existing_rows_for` below adds three rows per board: two that share
    entrants with the movable pool (exercising the rest-window half) and one
    that shares NO entrant but sits on a board court (exercising the
    gap-only half in isolation).
  section 8 (build-encode.ts:335, order dependencies) — NOW MODELED, the
    MOVABLE-DEPENDENT / MOVABLE-FEEDER case only (both `i` and `j` are new
    fixtures). z3 also encodes two mixed cases — a movable dependent against
    an immovable feeder, and a movable feeder against an immovable dependent
    — neither of which this bench's generated dependency pairs exercise
    (`dependency_pairs_for` only pairs two movable fixtures); the code below
    states the rule generally but the SWEEP does not exercise the mixed
    forms. Semantics: the dependent may not START until the feeder's END
    plus the DEPENDENT's own `effectiveRestMinutes` (resolved off the
    dependent's own row, placement-free) — this bench reuses the same
    `hard_rest_min` constant every other rest check in this script uses,
    which is the file's standing simplification of `effectiveRestMinutes`.
  section 9 (build-encode.ts:385, typed instruction rules): only
    `max_fixtures_per_day` is modeled, unchanged from the first pass.

--- one deliberate SIMPLIFICATION carried over from the first pass ----------

bench-build.ts gives every fixture a `people: [p-home, p-away]` list that is
a 1:1 shadow of the entrant id. z3 states rest over BOTH entrant-keyed and
person-keyed groups; on this bench board the two namespaces cannot diverge,
so the person-keyed groups are exact duplicates and this script states the
rest window over entrant groups only (section 6 AND the T2 idle-gap proxy
below).

--- objective: the FULL T0-T3 lexicographic chain, ported as SEQUENTIAL
    NATIVE CP-SAT SOLVES rather than z3's push/pop bound-walk ----------------

z3 walks each tier under `solver.push()/pop()`: assert "strictly better than
the incumbent", check, take SAT boards until one bound comes back UNSAT (the
proof of that tier's optimum), then freeze it and move to the next tier.
CP-SAT has no direct equivalent of z3's incremental push/pop, but it DOES
have native `Minimize`/`Maximize` within one solve — so instead of walking
bounds one unit at a time, each tier here is ONE CP-SAT solve with the right
objective, and the tier's ACHIEVED value (whether proved optimal or only the
best found before its slice of the wall ran out — the same "freeze what was
achieved, not what was proved" rule z3's own header documents for the
`unknown` case) is frozen as a `>=`/`<=` bound for every later tier by
rebuilding the model with that bound added. Four tiers, four fresh
`CpModel`s, one shared wall-clock deadline threaded across all four calls
(`run_full_chain`) — the sequential-solve pattern is exactly what plain
CP-SAT usage looks like for this kind of problem.

T1 (makespan) and T3 (court imbalance) get an EXACT native term, the same
"squeeze two free integers onto the true extremes" trick z3 uses
(`mk_hi - mk_lo`, `cb_hi - cb_lo`) — see build.ts:2065-2135.

T2 (worst idle gap) does NOT get z3's exact encoding. z3 states it as a
BOUND-parameterized clause family (occ/tail chain over start indices) rather
than an arithmetic term, precisely because "which pair of a participant's
matches are CONSECUTIVE" depends on the assignment and needs an O(n^2)-ish
ordering argument to state honestly (build.ts:2137-2235's docstring proves
the soundness/completeness of that specific shape). This script instead
states a genuine CP-SAT arithmetic term that is EXACT when a participant has
at most 2 fixtures — true for effectively every entrant on this bench board,
since `per_entrant=2` is fixed for the whole sweep — and a safe OVER-approx-
imation otherwise: `worst_gap = max over ALL pairs (not just consecutive
ones) in a participant's group of max(0, |start_i - start_j| - duration)`,
each pair's diff only enforced when BOTH fixtures are placed
(`OnlyEnforceIf([placed[i], placed[j]])`). For any group with 3+ fixtures
(possible at the pool-size rounding boundary) this can OVER-count — a
non-consecutive pair's gap could exceed the true worst CONSECUTIVE gap — so
the solver may be pushed to avoid boards that are actually fine. That errs
toward a WORSE reported idle-gap number, never a better one, so it cannot
make this script's numbers look more favorable than z3's real encoding
would; it is flagged here so nobody mistakes it for the byte-exact port the
other three tiers are.

--- REWRITE v2: interval vars + AddNoOverlap replace the slot-boolean grid ---

v1 (above, and everything it describes) encoded placement as one Bool per
(fixture, slot) pair plus AtMostOne constraint families over discretized time
windows (`windows_over`) for the turnaround-gap and participant-rest rules.
At gap_min=10 the lattice re-spacing to a 10-minute tick (see "court-
turnaround gap" above) blew section 6 up to 16,838-48,232 AtMostOne
constraints over 79k-283k Bool vars depending on board size, and CP-SAT
returned UNKNOWN (zero incumbents in the whole wall) on every board this
script sweeps at >=160 fixtures, AND on the 37-fixture production board even
at a full 20s wall with T0 alone — see `full_sweep_v3.log`.

`build_model` below replaces that encoding with CP-SAT's purpose-built
scheduling primitives:
  * ONE `NewIntVar` `start[i]` per fixture (domain = the admissible tick
    starts for that fixture — a single pinned value for notAfter fixtures,
    every tick otherwise) instead of a Bool per (fixture, slot).
  * ONE `NewOptionalFixedSizeIntervalVar` per (fixture, court), sized
    `matchMinutes + gapMinutes`, presence = "fixture placed on this court",
    all sharing that fixture's single `start[i]` (no channeling needed: the
    interval simply free-floats with `start[i]` whenever its own presence
    literal is off). `AddNoOverlap` per court replaces sections 2 AND 3 in
    one shot — a court no-overlap at width dur+gap is a strict superset of
    exact-slot exclusivity at width dur, so section 2 (plain AtMostOne per
    slot) is now provably redundant and dropped.
  * ONE `NewOptionalFixedSizeIntervalVar` per fixture, sized
    `matchMinutes + perEntrantMinRest`, presence = `placed[i]`. `AddNoOverlap`
    per entrant group (cross-court) replaces section 6's `windows_over` walk.
  * existing/pinned rows (section 7) become plain `NewFixedSizeIntervalVar`
    (no presence literal — always in the model) folded into the SAME
    per-court and per-entrant NoOverlap families the movable fixtures use.
    This drops section 7's separate per-slot forbid-list pass entirely: a
    fixed interval cannot move, so NoOverlap against it already gives the
    one-directional "existing blocks movable, never the reverse" behavior
    the old section 7 docstring called for by construction — verified
    algebraically equivalent to the old `reach = max(gap_ms, r_ms)` window
    (both reduce to "forbid iff (same_court AND within gap) OR (shares
    entrant AND within rest)").
  * section 8 (order deps) and T1/T2/T3 now read `start[i]` directly instead
    of summing a one-hot row of slot Bools — same arithmetic, fewer terms.

This is a genuinely different model shape, not a tweak: v1's `place` dict and
`windows_over` are gone from the hot path (the latter is still defined below,
now dead code, kept only for provenance/diff-legibility). See the bottom of
this file / the dispatch report for the head-to-head numbers against v1.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass

from ortools.sat.python import cp_model

# Board generation lives in placement_bench_boards so the service's own tests can
# drive the SAME generator instead of a second, drifting copy (the exact class
# of bug the REFLOW investigation hit with `syntheticBoard()`). Everything
# imported here was defined in this file until that extraction and is
# unchanged by it.
from placement_bench_boards import (
    DAY_MS,
    DIVISIONS,
    GAP_MIN,
    MATCH_MIN,
    MIN_MS,
    Board,
    Existing,
    Fixture,
    build_board,
    dependency_pairs_for,
    entrant_of,
    existing_rows_for,
    intervals_overlap,
)


def greedy_seed(
    board: Board,
    gap_min: int,
    existing: list[Existing],
) -> dict[int, int]:
    """A cheap greedy placement — NOT a CP-SAT construct at all, just a plain
    Python first-fit walk — used only to warm-start CP-SAT via `AddHint`
    (threaded in through `build_model`'s `hint` param; the caller-facing
    switch is `run_full_chain`'s `warm_start`). Mirrors z3's own design
    principle (build.ts's header: "Greedy seeds the incumbent... lets this
    ship with NO escape hatch back to greedy") — CP-SAT gets no such seed by
    default and searches cold every solve, which the profiled sweep showed
    costing it dearly at this model's scale.

    Respects sections 2 (slot exclusivity), 3 (court gap), 6 (rest, entrant
    only), 7 (existing rows) and 9 (day cap) — the constraints a first-fit
    walk can check cheaply with running interval lists. Does NOT respect
    section 8 (order dependencies): a hint only needs to be a GOOD prior, not
    a valid solution — CP-SAT's hint mechanism tolerates a partially invalid
    hint and repairs or discards it rather than failing the solve.
    """
    dur_ms = MATCH_MIN * MIN_MS
    gap_ms = gap_min * MIN_MS
    rest_ms = board.hard_rest_min * MIN_MS
    slots = board.slots
    n_slots = len(slots)
    starts = [s.start_ms for s in slots]
    by_start = sorted(range(n_slots), key=lambda s: (starts[s], s))

    first_start = min(starts) if n_slots > 0 else 0
    pinned_entrants: set[str] = set()
    if board.not_after_entrants > 0:
        pinned_entrants = {entrant_of(board.pool - 1 - k) for k in range(min(board.not_after_entrants, board.pool))}

    def candidates_for(fx: Fixture) -> list[int]:
        if fx.home in pinned_entrants or fx.away in pinned_entrants:
            return [s for s in range(n_slots) if starts[s] == first_start]
        return by_start

    slot_occupied = [False] * n_slots
    court_intervals: dict[str, list[tuple[int, int]]] = {}
    entrant_intervals: dict[str, list[tuple[int, int]]] = {}
    day_count: dict[tuple[str, int], int] = {}
    placement: dict[int, int] = {}

    for i, fx in enumerate(board.fixtures):
        for s in candidates_for(fx):
            if slot_occupied[s]:
                continue
            st = starts[s]
            end = st + dur_ms
            court = slots[s].court
            ok = True
            for os_, oe in court_intervals.get(court, ()):
                if intervals_overlap(st, end + gap_ms, os_, oe + gap_ms):
                    ok = False
                    break
            if ok:
                for e in (fx.home, fx.away):
                    for os_, oe in entrant_intervals.get(e, ()):
                        if intervals_overlap(st, end + rest_ms, os_, oe + rest_ms):
                            ok = False
                            break
                    if not ok:
                        break
            if ok:
                for ex in existing:
                    shares = fx.home in ex.entrants or fx.away in ex.entrants
                    same_court = ex.court == court
                    if not shares and not same_court:
                        continue
                    r_ms = rest_ms if shares else 0
                    compatible = True
                    if same_court and intervals_overlap(st, end + gap_ms, ex.start_ms, ex.end_ms + gap_ms):
                        compatible = False
                    if compatible and shares and intervals_overlap(st, end + r_ms, ex.start_ms, ex.end_ms + r_ms):
                        compatible = False
                    if not compatible:
                        ok = False
                        break
            if not ok:
                continue
            day = int(st // DAY_MS)
            key = (fx.division, day)
            if day_count.get(key, 0) >= board.day_cap:
                continue
            placement[i] = s
            slot_occupied[s] = True
            court_intervals.setdefault(court, []).append((st, end))
            entrant_intervals.setdefault(fx.home, []).append((st, end))
            entrant_intervals.setdefault(fx.away, []).append((st, end))
            day_count[key] = day_count.get(key, 0) + 1
            break
    return placement


# --- windows_over: build-encode.ts's sliding-window walk, verbatim shape ---
# DEAD CODE as of the REWRITE v2 interval-based `build_model` below — v2
# replaces every AtMostOne-over-windows_over-output family with AddNoOverlap
# over interval vars. Kept only so the v1 shape (and the diff against it)
# stays legible; nothing in the current hot path calls this.


def windows_over(order: list[int], start_of, width_ms: int):
    prev_end = -1
    n = len(order)
    a = 0
    while a < n:
        b = a
        while b + 1 < n and start_of(order[b + 1]) < start_of(order[a]) + width_ms:
            b += 1
        if not (a > 0 and b == prev_end):
            prev_end = b
            yield order[a : b + 1]
        a += 1


# --- CP-SAT model ------------------------------------------------------------

Tier = str  # "placed" | "makespan" | "idlegap" | "imbalance"


def build_model(
    board: Board,
    *,
    gap_min: int,
    existing: list[Existing],
    dependencies: list[tuple[str, str]],
    tier: Tier,
    placed_floor: int | None = None,
    makespan_ceiling: int | None = None,
    idlegap_ceiling: int | None = None,
    hint: dict[int, int] | None = None,
):
    """Build the FULL constraint model fresh (sections 1,2,3,5,6,7,8,9) and
    set ONE tier's objective, optionally floored/ceilinged by every earlier
    tier's ACHIEVED value. Called once per tier by `run_full_chain` — see the
    module docstring for why a fresh model per tier stands in for z3's
    push/pop walk.

    REWRITE v2 (see module docstring "REWRITE v2" section): interval vars +
    AddNoOverlap replace the v1 fixture-x-slot Boolean grid. `place`/`by_slot`
    /`occ_any`/`windows_over` are gone from this function; sections 2 and 3
    collapse into one per-court `AddNoOverlap` family, section 6 into one
    per-entrant `AddNoOverlap` family, and section 7's forbid-list pass is
    gone (existing rows are plain fixed intervals folded into those same two
    families)."""
    model = cp_model.CpModel()
    n = len(board.fixtures)
    dur_ms = MATCH_MIN * MIN_MS
    gap_ms = gap_min * MIN_MS
    rest_ms = board.hard_rest_min * MIN_MS

    admissible_starts = sorted({s.start_ms for s in board.slots})
    first_start = admissible_starts[0] if admissible_starts else 0
    full_domain = cp_model.Domain.FromValues(admissible_starts or [0])
    pinned_domain = cp_model.Domain.FromValues([first_start])

    pinned_entrants: set[str] = set()
    if board.not_after_entrants > 0:
        pinned_entrants = {entrant_of(board.pool - 1 - k) for k in range(min(board.not_after_entrants, board.pool))}

    # One IntVar per fixture (its actual chosen start, domain-restricted to
    # admissible ticks — a single pinned value for notAfter fixtures) instead
    # of a Bool per (fixture, slot). Replaces v1's `place`/`domain_for`.
    placed = [model.NewBoolVar(f"p_{i}") for i in range(n)]
    start: list[cp_model.IntVar] = []
    presence_court: list[dict[str, cp_model.IntVar]] = []
    for i, fx in enumerate(board.fixtures):
        dom = pinned_domain if (fx.home in pinned_entrants or fx.away in pinned_entrants) else full_domain
        start.append(model.NewIntVarFromDomain(dom, f"start_{i}"))
        pcs = {c: model.NewBoolVar(f"onc_{i}_{c}") for c in board.courts}
        presence_court.append(pcs)
        # section 1: at most one court, and `placed[i]` tracks it exactly.
        model.Add(sum(pcs.values()) == placed[i])

    # sections 2+3 fused: one optional interval per (fixture, court), sized
    # matchMinutes+gapMinutes, all sharing that fixture's single `start[i]`.
    # AddNoOverlap per court is a strict superset of exact-slot exclusivity
    # (section 2) at this width, so section 2 needs no separate statement.
    width_court = dur_ms + gap_ms
    interval_court: list[dict[str, cp_model.IntervalVar]] = [dict() for _ in range(n)]
    court_lists: dict[str, list[cp_model.IntervalVar]] = {c: [] for c in board.courts}
    for i in range(n):
        for c in board.courts:
            iv = model.NewOptionalFixedSizeIntervalVar(start[i], width_court, presence_court[i][c], f"ivc_{i}_{c}")
            interval_court[i][c] = iv
            court_lists[c].append(iv)

    # section 7 (court-gap half): existing rows are plain fixed intervals,
    # same width, folded into their own court's list — one-directional by
    # construction (a fixed interval cannot move to accommodate a movable
    # one; only the movable side is ever constrained by NoOverlap here).
    for e in existing:
        if e.court in court_lists:
            court_lists[e.court].append(model.NewFixedSizeIntervalVar(e.start_ms, width_court, f"ivc_existing_{e.id}"))

    n_gap_windows = 0
    for lst in court_lists.values():
        if len(lst) >= 2:
            model.AddNoOverlap(lst)
            n_gap_windows += 1

    # section 6: participant rest window, entrant-keyed groups only. One
    # optional interval per fixture, sized matchMinutes+hard_rest_min,
    # presence=placed[i]; AddNoOverlap per entrant group (cross-court)
    # replaces the old windows_over/AtMostOne walk outright.
    by_entrant: dict[str, list[int]] = {}
    for i, fx in enumerate(board.fixtures):
        by_entrant.setdefault(fx.home, []).append(i)
        by_entrant.setdefault(fx.away, []).append(i)

    width_rest = dur_ms + rest_ms
    interval_rest = [
        model.NewOptionalFixedSizeIntervalVar(start[i], width_rest, placed[i], f"ivr_{i}") for i in range(n)
    ]

    # section 7 (rest half): existing rows sharing an entrant with the
    # movable pool get ONE fixed interval each, added into that entrant's
    # NoOverlap list only — algebraically equivalent to v1's one-directional
    # `reach = max(gap_ms, r_ms)` window (both reduce to "forbid iff
    # (same_court AND within gap) OR (shares entrant AND within rest)"; the
    # court half is handled above, this is the entrant half).
    existing_rest_by_entrant: dict[str, list[cp_model.IntervalVar]] = {}
    for e in existing:
        iv = None
        for ent in e.entrants:
            if ent in by_entrant:
                if iv is None:
                    iv = model.NewFixedSizeIntervalVar(e.start_ms, width_rest, f"ivr_existing_{e.id}")
                existing_rest_by_entrant.setdefault(ent, []).append(iv)

    n_rest_constraints = 0
    for ent, group in by_entrant.items():
        lst = [interval_rest[i] for i in group] + existing_rest_by_entrant.get(ent, [])
        if len(lst) >= 2:
            model.AddNoOverlap(lst)
            n_rest_constraints += 1

    # section 8: order dependencies, movable-dependent/movable-feeder case.
    # `start[i]` is already the fixture's real chosen start (no one-hot sum
    # needed the way v1's `start_expr` required), so this is unchanged in
    # spirit — one linear inequality per dependency pair, O(1) to build.
    id_to_idx = {fx.id: i for i, fx in enumerate(board.fixtures)}
    n_dep_constraints = 0
    for dep_id, feed_id in dependencies:
        i = id_to_idx.get(dep_id)
        j = id_to_idx.get(feed_id)
        if i is None or j is None:
            continue
        model.Add(start[i] >= start[j] + dur_ms + rest_ms).OnlyEnforceIf([placed[i], placed[j]])
        n_dep_constraints += 1

    # section 9: max_fixtures_per_day, per division. Day membership is a
    # range-reified indicator: calendar days partition the admissible-start
    # domain exactly (no two days' ms ranges overlap), so `on_day[i][d]==1
    # => start[i] in [d*DAY_MS, (d+1)*DAY_MS)` plus `sum_d on_day[i][d] ==
    # placed[i]` is enough to pin down the true day one-directionally — the
    # solver cannot "cheat" by setting the wrong day's bool, since turning it
    # on binds the range constraint for real.
    day_ids = sorted({v // DAY_MS for v in admissible_starts}) if admissible_starts else [0]
    on_day: list[dict[int, cp_model.IntVar]] = [dict() for _ in range(n)]
    for i in range(n):
        for d in day_ids:
            lo, hi = d * DAY_MS, (d + 1) * DAY_MS
            b = model.NewBoolVar(f"day_{i}_{d}")
            model.Add(start[i] >= lo).OnlyEnforceIf(b)
            model.Add(start[i] < hi).OnlyEnforceIf(b)
            on_day[i][d] = b
        model.Add(sum(on_day[i].values()) == placed[i])
    for division in DIVISIONS:
        fx_idx = [i for i, fx in enumerate(board.fixtures) if fx.division == division]
        for d in day_ids:
            lits = [on_day[i][d] for i in fx_idx]
            if lits:
                model.Add(sum(lits) <= board.day_cap)

    placed_sum = sum(placed)
    if placed_floor is not None:
        model.Add(placed_sum >= placed_floor)

    max_end = (max(admissible_starts) + dur_ms) if admissible_starts else dur_ms

    # T1: makespan, exact native term (mk_hi - mk_lo), squeezed onto the true
    # extremes exactly as build.ts:2075-2082 does — now directly off
    # `start[i]`/`placed[i]`, no `occ_any` sum-over-slot needed.
    mk_lo = model.NewIntVar(0, max_end, "mk_lo")
    mk_hi = model.NewIntVar(0, max_end, "mk_hi")
    for i in range(n):
        model.Add(mk_lo <= start[i]).OnlyEnforceIf(placed[i])
        model.Add(mk_hi >= start[i] + dur_ms).OnlyEnforceIf(placed[i])
    makespan = mk_hi - mk_lo
    if makespan_ceiling is not None:
        model.Add(makespan <= makespan_ceiling)

    # T3: court imbalance, exact native term (cb_hi - cb_lo), same squeeze
    # technique as build.ts:2115-2135 — court load is now a direct sum of
    # `presence_court[i][c]` rather than a sum over that court's occ_any
    # slots.
    cb_lo = model.NewIntVar(0, n * dur_ms, "cb_lo")
    cb_hi = model.NewIntVar(0, n * dur_ms, "cb_hi")
    for c in board.courts:
        load = sum(presence_court[i][c] for i in range(n)) * dur_ms
        model.Add(cb_hi >= load)
        model.Add(cb_lo <= load)
    imbalance = cb_hi - cb_lo

    # T2: worst idle gap — APPROXIMATE native term, see module docstring.
    # Exact for any participant with <=2 fixtures (true for ~every entrant
    # on this bench board); a safe over-approximation for 3+ (max over ALL
    # pairs, not just consecutive ones). Uses `start[i]` directly.
    diff_vars: list[cp_model.IntVar] = []
    for group in by_entrant.values():
        if len(group) < 2:
            continue
        for x in range(len(group)):
            for y in range(x + 1, len(group)):
                i, j = group[x], group[y]
                d = model.NewIntVar(0, max_end, f"gap_{i}_{j}")
                model.Add(d >= start[i] - start[j] - dur_ms).OnlyEnforceIf([placed[i], placed[j]])
                model.Add(d >= start[j] - start[i] - dur_ms).OnlyEnforceIf([placed[i], placed[j]])
                diff_vars.append(d)
    worst_gap = model.NewIntVar(0, max_end, "worst_gap")
    if diff_vars:
        model.AddMaxEquality(worst_gap, diff_vars)
    else:
        model.Add(worst_gap == 0)
    if idlegap_ceiling is not None:
        model.Add(worst_gap <= idlegap_ceiling)

    if tier == "placed":
        model.Maximize(placed_sum)
    elif tier == "makespan":
        model.Minimize(makespan)
    elif tier == "idlegap":
        model.Minimize(worst_gap)
    elif tier == "imbalance":
        model.Minimize(imbalance)
    else:
        raise ValueError(f"unknown tier {tier!r}")

    # Warm-start: hand CP-SAT the greedy placement as a solution hint instead
    # of making it search cold every tier. `greedy_seed` still returns
    # dict[fixture_index] -> slot INDEX (board.slots), so translate each hint
    # into (start[i]=that slot's start_ms, presence_court[i][that court]=1,
    # placed[i]=1) — same "only positive decisions hinted" contract as v1.
    if hint:
        for i, s in hint.items():
            sl = board.slots[s]
            model.add_hint(start[i], sl.start_ms)
            model.add_hint(placed[i], 1)
            model.add_hint(presence_court[i][sl.court], 1)

    aux = {
        "placed_sum": placed_sum,
        "makespan": makespan,
        "worst_gap": worst_gap,
        "imbalance": imbalance,
        "num_bool_vars": n + sum(len(pc) for pc in presence_court),  # placed[i] + presence_court bools; start[i] are IntVars, not bools
        "n_rest_constraints": n_rest_constraints,
        "n_gap_windows": n_gap_windows,
        "n_existing_forbids": 0,  # subsumed into the NoOverlap families directly in v2; no separate forbid-list pass
        "n_dep_constraints": n_dep_constraints,
        "start": start,
        "presence_court": presence_court,
    }
    return model, None, placed, aux


def run_full_chain(
    board: Board,
    wall_s: float,
    gap_min: int,
    existing: list[Existing],
    dependencies: list[tuple[str, str]],
    tiers: tuple[Tier, ...] = ("placed", "makespan", "idlegap", "imbalance"),
    warm_start: bool = True,
) -> list[dict]:
    """Sequential-solve port of z3's lexicographic T0-T3 walk (see module
    docstring): one native CP-SAT solve per tier, each flooring/ceiling-ing
    on the previous tier's ACHIEVED value, all sharing ONE wall-clock
    deadline. `tiers=("placed",)` reproduces the first pass's T0-only run for
    the isolation comparison (task item 4).

    `warm_start`: compute ONE greedy placement (`greedy_seed`) up front and
    hand it to every tier's solve as a `model.AddHint` — mirrors z3's own
    "greedy seeds the incumbent" design (build.ts's header). Computed once,
    outside the deadline-charged per-tier loop, since it does not depend on
    any tier's floor/ceiling and is cheap (plain Python, no CP-SAT).
    """
    deadline = time.perf_counter() + wall_s
    placed_floor: int | None = None
    makespan_ceiling: int | None = None
    idlegap_ceiling: int | None = None
    hint = greedy_seed(board, gap_min, existing) if warm_start else None
    rows: list[dict] = []
    for tier in tiers:
        remaining_before_build = deadline - time.perf_counter()
        if remaining_before_build <= 0.05:
            rows.append({"tier": tier, "status": "SKIPPED_NO_TIME"})
            break
        t_build0 = time.perf_counter()
        model, _place, _placed, aux = build_model(
            board,
            gap_min=gap_min,
            existing=existing,
            dependencies=dependencies,
            tier=tier,
            placed_floor=placed_floor,
            makespan_ceiling=makespan_ceiling,
            idlegap_ceiling=idlegap_ceiling,
            hint=hint,
        )
        build_time_s = time.perf_counter() - t_build0

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(0.05, deadline - time.perf_counter())
        solver.parameters.num_search_workers = 8
        # Same two knobs as the first pass, same reason (diag.py): symmetry
        # detection and probing presolve alone can burn the entire wall
        # before one branch runs, at these board sizes.
        solver.parameters.symmetry_level = 0
        solver.parameters.cp_model_probing_level = 0
        t_solve0 = time.perf_counter()
        status = solver.Solve(model)
        solve_time_s = time.perf_counter() - t_solve0
        status_name = solver.StatusName(status)

        row: dict = {
            "tier": tier,
            "status": status_name,
            "build_time_s": round(build_time_s, 3),
            "solve_time_s": round(solve_time_s, 3),
            "num_bool_vars": aux["num_bool_vars"],
            "n_rest_constraints": aux["n_rest_constraints"],
            "n_gap_windows": aux["n_gap_windows"],
            "n_existing_forbids": aux["n_existing_forbids"],
            "n_dep_constraints": aux["n_dep_constraints"],
        }
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            placed_val = int(solver.Value(aux["placed_sum"]))
            makespan_val = int(solver.Value(aux["makespan"]))
            idlegap_val = int(solver.Value(aux["worst_gap"]))
            imbalance_val = int(solver.Value(aux["imbalance"]))
            row.update(
                placed=placed_val,
                makespan_ms=makespan_val,
                idlegap_ms=idlegap_val,
                imbalance_ms=imbalance_val,
            )
            if tier == "placed":
                placed_floor = placed_val
            elif tier == "makespan":
                makespan_ceiling = makespan_val
            elif tier == "idlegap":
                idlegap_ceiling = idlegap_val
            rows.append(row)
        else:
            rows.append(row)
            break
    return rows


# --- sweep driver ------------------------------------------------------------


@dataclass
class SweepPoint:
    label: str
    n: int
    courts: int
    target_slots: int
    wall_s: float
    per_entrant: int = 2
    not_after_entrants: int = 2
    blackouts: bool = False
    skip_weekends: bool = False
    mode: str = "chain"  # "chain" (T0-T3) | "t0_only" (T0 alone, for item 4)


def run_point(pt: SweepPoint) -> dict:
    board = build_board(
        n=pt.n,
        courts_n=pt.courts,
        target_slots=pt.target_slots,
        per_entrant=pt.per_entrant,
        not_after_entrants=pt.not_after_entrants,
        blackouts=pt.blackouts,
        skip_weekends=pt.skip_weekends,
    )
    existing = existing_rows_for(board)
    dependencies = dependency_pairs_for(board)
    tiers: tuple[Tier, ...] = ("placed",) if pt.mode == "t0_only" else ("placed", "makespan", "idlegap", "imbalance")

    real_slots = len(board.slots)
    t0 = time.perf_counter()
    tier_rows = run_full_chain(board, pt.wall_s, GAP_MIN, existing, dependencies, tiers=tiers)
    total_wall = time.perf_counter() - t0

    last = tier_rows[-1] if tier_rows else {}
    tiers_completed = sum(1 for r in tier_rows if r.get("status") in ("OPTIMAL", "FEASIBLE"))
    total_build_s = round(sum(r.get("build_time_s", 0.0) for r in tier_rows), 3)
    total_solve_s = round(sum(r.get("solve_time_s", 0.0) for r in tier_rows), 3)

    row = {
        "label": pt.label,
        "mode": pt.mode,
        "n": pt.n,
        "courts": pt.courts,
        "target_slots": pt.target_slots,
        "real_slots": real_slots,
        "fixture_slots": pt.n * real_slots,
        "days": board.days,
        "calendar_days_spanned": board.calendar_days_spanned,
        "dropped_by_blackout": board.dropped_by_blackout,
        "slots_per_court_day": board.slots_per_court_day,
        "day_cap": board.day_cap,
        "blackouts": pt.blackouts,
        "skip_weekends": pt.skip_weekends,
        "gap_min": GAP_MIN,
        "num_existing": len(existing),
        "num_dependencies": len(dependencies),
        "wall_budget_s": pt.wall_s,
        "tiers_completed": tiers_completed,
        "tiers_requested": len(tiers),
        "last_tier": last.get("tier"),
        "last_status": last.get("status"),
        "placed": last.get("placed"),
        "total": pt.n,
        "makespan_ms": last.get("makespan_ms"),
        "idlegap_ms": last.get("idlegap_ms"),
        "imbalance_ms": last.get("imbalance_ms"),
        "total_build_s": total_build_s,
        "total_solve_s": total_solve_s,
        "total_wall_s": round(total_wall, 3),
        "num_bool_vars": last.get("num_bool_vars"),
        "tier_rows": tier_rows,
    }
    print(json.dumps({k: v for k, v in row.items() if k != "tier_rows"}), flush=True)
    return row


def main() -> None:
    points = [
        SweepPoint("knee-140x144", n=140, courts=4, target_slots=144, wall_s=8.0),
        SweepPoint("knee-160x216", n=160, courts=4, target_slots=216, wall_s=8.0),
        SweepPoint("beyond-200x300", n=200, courts=4, target_slots=300, wall_s=8.0),
        SweepPoint("beyond-300x400", n=300, courts=4, target_slots=400, wall_s=8.0),
        SweepPoint("prod-37x77k@8s", n=37, courts=5, target_slots=2081, wall_s=8.0),
        SweepPoint("prod-37x77k@20s", n=37, courts=5, target_slots=2081, wall_s=20.0),
        SweepPoint("knee-140x144+bo", n=140, courts=4, target_slots=144, wall_s=8.0, blackouts=True, skip_weekends=True),
        SweepPoint("knee-160x216+bo", n=160, courts=4, target_slots=216, wall_s=8.0, blackouts=True, skip_weekends=True),
        SweepPoint("beyond-200x300+bo", n=200, courts=4, target_slots=300, wall_s=8.0, blackouts=True, skip_weekends=True),
        SweepPoint("beyond-300x400+bo", n=300, courts=4, target_slots=400, wall_s=8.0, blackouts=True, skip_weekends=True),
        SweepPoint("prod-37x77k+bo@8s", n=37, courts=5, target_slots=2081, wall_s=8.0, blackouts=True, skip_weekends=True),
        SweepPoint("prod-37x77k+bo@20s", n=37, courts=5, target_slots=2081, wall_s=20.0, blackouts=True, skip_weekends=True),
        # item 4: T0-only vs full T0-T3 chain, same constraint set, production shape.
        SweepPoint("prod-37x77k@8s-t0only", n=37, courts=5, target_slots=2081, wall_s=8.0, mode="t0_only"),
        SweepPoint("prod-37x77k@20s-t0only", n=37, courts=5, target_slots=2081, wall_s=20.0, mode="t0_only"),
    ]

    only = sys.argv[1] if len(sys.argv) > 1 else None
    results = []
    for pt in points:
        if only is not None and only not in pt.label:
            continue
        print(
            f"--- running {pt.label} (n={pt.n}, courts={pt.courts}, target_slots={pt.target_slots}, "
            f"wall={pt.wall_s}s, mode={pt.mode}, blackouts={pt.blackouts}, skip_weekends={pt.skip_weekends}) ---",
            file=sys.stderr,
            flush=True,
        )
        results.append(run_point(pt))

    print("\n=== SUMMARY ===")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
